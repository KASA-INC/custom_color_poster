"""
Koteplakat - Shopify -> Gelato webhook-tjeneste
================================================
Samme arkitektur/mønster som Måneplakat sin app.py (FastAPI, HMAC,
S3-kompatibel lagring, Gelato v4) - men:
  - leser "Bakgrunnsfarge (hex)" og "Kartstil" fra line item properties,
    IKKE Dato/Sted/Tekst
  - komponerer en flat bakgrunnsfarge BAK en allerede eksisterende,
    transparent master-PDF (koteplakat_render.py), IKKE tegner et bilde
    fra bunnen
  - kilde-PDF-ene (Mørkt/Lyst per motiv) hentes fra R2 sin "kilder/"-mappe
    (SAMME bucket som Måneplakat, egen prefix) - se
    last_opp_kildefiler.py for opplasting dit

Kjør: uvicorn koteplakat_app:app  (Render/Railway/Fly/VPS - alltid på)
Alle hemmeligheter settes som miljøvariabler (se README - samme mønster
som Måneplakat, pluss GELATO_PRODUCT_UID_50X70 for koteplakat sitt
spesifikke Gelato-produkt).

VIKTIG: Skru AV Gelatos innebygde Shopify-kobling for disse produktene,
ellers opprettes ordrene to ganger.
"""
import base64, hashlib, hmac, json, logging, os, threading

import boto3
import requests
from fastapi import FastAPI, Request, Response, BackgroundTasks

import koteplakat_render

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("koteplakat")

# --- Konfig (miljøvariabler) - samme navnekonvensjon som Måneplakat der det er delt ---
SHOPIFY_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
GELATO_API_KEY = os.environ.get("GELATO_API_KEY", "")
GELATO_URL     = "https://order.gelatoapis.com/v4/orders"
SHIPMENT       = os.environ.get("GELATO_SHIPMENT_METHOD", "normal")
DRY_RUN        = os.environ.get("DRY_RUN", "0") == "1"

# Gelato-produkt-UID for koteplakat sitt 50x70-format - MERK: dette er IKKE
# nødvendigvis samme UID som Måneplakat bruker for sin egen 50x70, selv om
# størrelsen er lik - papirvalg/finish kan avvike. Sett riktig verdi selv
# fra Gelato Dashboard før DRY_RUN settes til 0.
GELATO_PRODUCT_UID_50X70 = os.environ.get(
    "GELATO_PRODUCT_UID_50X70",
    "flat_500x700-mm-20x28-inch_170-gsm-65lb-uncoated_4-0_ver",  # PLASSHOLDER - bekreft i Gelato Dashboard
)

# S3-kompatibel lagring (SAMME bucket som Måneplakat, egen prefix for kildefiler)
S3_BUCKET   = os.environ.get("S3_BUCKET", "")
S3_REGION   = os.environ.get("S3_REGION", "auto")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or None
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
KILDER_PREFIX = os.environ.get("KILDER_PREFIX", "kilder")   # der transparente master-PDF-er ligger

app = FastAPI()


def verify_hmac(raw: bytes, header: str) -> bool:
    if not SHOPIFY_SECRET or not header:
        return False
    digest = hmac.new(SHOPIFY_SECRET.encode(), raw, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header)


def props_to_dict(line_item) -> dict:
    return {p.get("name", "").strip().lower(): (p.get("value") or "").strip()
            for p in (line_item.get("properties") or [])}


def normaliser_kartstil(kartstil: str) -> str:
    """Shopify sender 'Mørkt'/'Lyst' (med ø, fra koteplakat-fargevelger.liquid),
    men selve filnavnene på R2/disk bruker 'Morkt' UTEN ø (ASCII) - samme
    konvensjon som hele Python-pipelinen ellers (Moerke.py,
    splitt_transparent_master.py, osv). Normaliser her, IKKE i liquid-en -
    kunden skal fortsatt se 'Mørkt' korrekt stavet i grensesnittet."""
    k = kartstil.strip().lower()
    return "Lyst" if k.startswith("lys") else "Morkt"


def gyldig_hex(s: str) -> bool:
    s = (s or "").lstrip("#")
    if len(s) != 6:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _s3_klient():
    return boto3.client("s3", region_name=S3_REGION, endpoint_url=S3_ENDPOINT)


def hent_transparent_pdf(motiv: str, kartstil: str) -> bytes:
    """Henter riktig kilde-PDF fra R2 (kilder/{Motiv}_{Kartstil}_transparent.pdf).
    Kaster FileNotFoundError-lignende boto3-feil hvis motivet ikke finnes -
    fanges opp av kalleren, som hopper over linjen i stedet for å krasje
    hele ordre-behandlingen."""
    key = f"{KILDER_PREFIX}/{motiv}_{kartstil}_transparent.pdf"
    s3 = _s3_klient()
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return resp["Body"].read()


def upload_pdf(data: bytes, key: str) -> str:
    s3 = _s3_klient()
    extra = {"ContentType": "application/pdf"}
    if not S3_ENDPOINT:
        extra["ACL"] = "public-read"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, **extra)
    return f"{PUBLIC_BASE}/{key}"


# Serialiserer sjekk+merk innen samme instans. Viktig når Render våkner fra dvale:
# da leveres originalen og Shopifys retry(er) nesten samtidig, og uten lås kan
# begge passere allerede_behandlet() før noen har rukket å skrive markøren.
_KLAIM_LAAS = threading.Lock()


def _behandlet_key(order_id) -> str:
    return f"{KILDER_PREFIX}/_behandlede_ordre/{order_id}.json"


def allerede_behandlet(order_id) -> bool:
    """Sjekker om denne Shopify-ordren allerede er sendt til Gelato tidligere.

    NØDVENDIG fordi Shopify garantert kan levere samme webhook flere ganger
    (retries ved treg respons/cold start, eller flere webhook-abonnement på
    samme topic/URL uten at man vet det) - uten en slik sperre opprettes én
    Gelato-ordre PER levering, ikke én per faktisk Shopify-ordre. Lagres i
    S3/R2 (IKKE i minne) nettopp fordi denne tjenesten typisk kjører på en
    "sovende" instans (Render free/hobby) som kan starte helt på nytt mellom
    to leveringer av samme webhook - en in-memory sperre ville da ikke
    hjulpet i det hele tatt."""
    s3 = _s3_klient()
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=_behandlet_key(order_id))
        return True
    except Exception:
        return False


def fjern_markor(order_id):
    """Fjerner 'behandlet'-markøren igjen (brukes når Gelato-kallet feilet,
    slik at ordren kan sendes på nytt via 'Resend webhook' i Shopify)."""
    try:
        _s3_klient().delete_object(Bucket=S3_BUCKET, Key=_behandlet_key(order_id))
    except Exception as e:
        log.error("Klarte ikke fjerne markør for ordre %s: %s", order_id, e)


def merk_behandlet(order_id):
    s3 = _s3_klient()
    s3.put_object(Bucket=S3_BUCKET, Key=_behandlet_key(order_id),
                  Body=b"{}", ContentType="application/json")


def build_gelato_items(order) -> list:
    items = []
    for li in order.get("line_items", []):
        pr = props_to_dict(li)
        hex_farge = pr.get("bakgrunnsfarge (hex)") or pr.get("bakgrunnsfarge") or ""
        kartstil = normaliser_kartstil(pr.get("kartstil") or "Mørkt")
        motiv = li.get("title") or li.get("name") or ""

        if not gyldig_hex(hex_farge):
            continue  # ikke en koteplakat-linje (eller mangler egenskapen) -> hopp over

        try:
            transparent_bytes = hent_transparent_pdf(motiv, kartstil)
        except Exception as e:
            log.error("Fant ikke kilde-PDF for '%s' (%s): %s - hopper over linjen", motiv, kartstil, e)
            continue

        ferdig_pdf = koteplakat_render.komponer_bakgrunnsfarge_bytes(transparent_bytes, hex_farge)
        key = f"koteplakater/{order['id']}/{li['id']}.pdf"
        url = upload_pdf(ferdig_pdf, key)
        log.info("Komponerte %s (%s, %s) -> %s", motiv, kartstil, hex_farge, url)

        items.append({
            "itemReferenceId": str(li["id"]),
            "productUid": GELATO_PRODUCT_UID_50X70,
            "files": [{"type": "default", "url": url}],
            "quantity": int(li.get("quantity", 1)),
        })
    return items


def gelato_address(order) -> dict:
    a = order.get("shipping_address") or order.get("billing_address") or {}
    return {
        "firstName":   a.get("first_name", ""),
        "lastName":    a.get("last_name", ""),
        "companyName": a.get("company") or "",
        "addressLine1": a.get("address1", ""),
        "addressLine2": a.get("address2") or "",
        "city":        a.get("city", ""),
        "state":       a.get("province_code") or "",
        "postCode":    a.get("zip", ""),
        "country":     a.get("country_code", ""),
        "email":       order.get("email", ""),
        "phone":       a.get("phone") or order.get("phone") or "",
    }


def process_order(order: dict):
    order_id = order.get("id")
    with _KLAIM_LAAS:
        if allerede_behandlet(order_id):
            log.info("Ordre %s er allerede behandlet tidligere - hopper over "
                      "(duplikat webhook-levering fra Shopify). Ingen ny "
                      "Gelato-ordre opprettet.", order_id)
            return
        merk_behandlet(order_id)  # merk FØR vi begynner, inne i låsen

    lyktes = False
    try:
        lyktes = _send_til_gelato(order)
    except Exception:
        log.exception("Ordre %s: uventet feil under behandling", order_id)
    finally:
        if not lyktes:
            # Gelato avviste, ingen linjer, DRY_RUN eller krasj -> frigi markøren,
            # ellers kan ordren aldri sendes på nytt ('Resend webhook').
            fjern_markor(order_id)


def _send_til_gelato(order: dict) -> bool:
    """True KUN hvis Gelato faktisk godtok ordren."""
    items = build_gelato_items(order)
    if not items:
        log.info("Ordre %s: ingen koteplakat-linjer, hopper over.", order.get("id"))
        return False
    payload = {
        "orderType": "order",
        "orderReferenceId": f"shopify-{order['id']}",
        "customerReferenceId": str(order.get("customer", {}).get("id") or order.get("email", "")),
        "currency": order.get("currency", "NOK"),
        "items": items,
        "shipmentMethodUid": SHIPMENT,
        "shippingAddress": gelato_address(order),
    }
    if DRY_RUN:
        log.info("DRY_RUN - ville sendt til Gelato:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
        return False  # ingenting sendt -> ikke la markøren stå
    r = requests.post(GELATO_URL, headers={"Content-Type": "application/json",
                                           "X-API-KEY": GELATO_API_KEY},
                      data=json.dumps(payload), timeout=60)
    if r.status_code >= 300:
        log.error("Gelato-feil %s: %s", r.status_code, r.text)
        return False
    log.info("Gelato-ordre opprettet: %s", r.json().get("id"))
    return True


@app.post("/webhooks/orders-paid")
async def orders_paid(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not verify_hmac(raw, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        return Response(status_code=401, content="invalid hmac")
    order = json.loads(raw)
    h = request.headers
    # DIAGNOSE: viser hvilken tjeneste/instans og hvilken webhook som mottok ordren.
    # Samme Event-Id to ganger = Shopify leverte samme hendelse flere ganger (retry/abonnement).
    # Ulik Webhook-Id/Host/Render-tjeneste = flere abonnement eller flere tjenester.
    log.info("WEBHOOK mottatt: ordre=%s topic=%s event_id=%s webhook_id=%s host=%s "
             "render_tjeneste=%s render_instans=%s",
             order.get("id"), h.get("X-Shopify-Topic"), h.get("X-Shopify-Event-Id"),
             h.get("X-Shopify-Webhook-Id"), h.get("host"),
             os.environ.get("RENDER_SERVICE_NAME"), os.environ.get("RENDER_INSTANCE_ID"))
    bg.add_task(process_order, order)
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok", "produkt_uid": GELATO_PRODUCT_UID_50X70}

