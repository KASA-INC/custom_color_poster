"""
last_opp_kildefiler.py
--------------------------------
Laster opp ALLE transparente master-PDF-er (Mørkt + Lyst per motiv) til R2
sin "kilder/"-mappe (samme bucket som Måneplakat, egen prefix) - dette er
det webhook-tjenesten (koteplakat_app.py) faktisk henter fra når en
bestilling kommer inn.

Kjør denne HVER GANG et nytt motiv legges til / en eksisterende PDF
oppdateres - webhook-tjenesten leser alltid direkte fra R2, ikke fra noen
lokal disk (den kjører jo på en annen maskin/server).

Kjør (én mappe med alle *_transparent.pdf-filer, f.eks. GRUNNLAG/DATA/Transparent):
  python3 last_opp_kildefiler.py --inn-mappe "/full/sti/til/Transparent" \
    --bucket "din-bucket" --endpoint "https://xxxx.r2.cloudflarestorage.com"

Trenger samme miljøvariabler som webhook-tjenesten for selve R2-tilgangen
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY - boto3 sin standard).
"""
import argparse
from pathlib import Path

import boto3


def last_opp_alle(inn_mappe: str, bucket: str, endpoint: str, region: str = "auto",
                   kilder_prefix: str = "kilder", tor: bool = False):
    s3 = boto3.client("s3", region_name=region, endpoint_url=endpoint)
    inn = Path(inn_mappe)
    pdf_filer = sorted(inn.glob("*_transparent.pdf"))
    if not pdf_filer:
        raise SystemExit(f"Fant ingen *_transparent.pdf i {inn}")

    print(f"Fant {len(pdf_filer)} fil(er) å laste opp til s3://{bucket}/{kilder_prefix}/ ...")
    ok, feilet = 0, 0
    for pdf in pdf_filer:
        key = f"{kilder_prefix}/{pdf.name}"
        if tor:
            print(f"  [TØRRKJØRING] ville lastet opp: {pdf.name} -> {key}")
            ok += 1
            continue
        try:
            with open(pdf, "rb") as f:
                s3.put_object(Bucket=bucket, Key=key, Body=f.read(), ContentType="application/pdf")
            print(f"  OK: {pdf.name}")
            ok += 1
        except Exception as e:
            print(f"  FEILET: {pdf.name} - {e}")
            feilet += 1

    print(f"\nFerdig. {ok} OK, {feilet} feilet.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inn-mappe", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint", required=True, help="R2-endepunktet, f.eks. https://xxxx.r2.cloudflarestorage.com")
    parser.add_argument("--region", default="auto")
    parser.add_argument("--kilder-prefix", default="kilder")
    parser.add_argument("--tor", action="store_true", help="Vis kun hva som VILLE blitt lastet opp, ingen ekte opplasting")
    args = parser.parse_args()
    last_opp_alle(args.inn_mappe, args.bucket, args.endpoint, args.region, args.kilder_prefix, args.tor)


if __name__ == "__main__":
    main()
