"""
koteplakat_render.py - trykk-renderer for koteplakater
=========================================================
Til forskjell fra Måneplakat sin render_poster.py (som TEGNER hele bildet
fra bunnen for hver bestilling), komponerer denne KUN en flat bakgrunnsfarge
BAK en allerede eksisterende, ferdig transparent master-PDF (kotelinjer +
tittel, eksportert fra InDesign med lisensiert font - selve typografien
rendres ALDRI på nytt her, kun en fargeflate legges bak).

Kilde-PDF-ene (én Mørkt + én Lyst per motiv) hentes fra R2 sin "kilder/"-
mappe - se last_opp_kildefiler.py for hvordan disse lastes opp dit.
"""
import io

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


def _hex_til_rgb01(hex_str: str):
    hex_str = hex_str.lstrip("#")
    r, g, b = (int(hex_str[i:i + 2], 16) for i in (0, 2, 4))
    return r / 255, g / 255, b / 255


def komponer_bakgrunnsfarge_bytes(transparent_pdf_bytes: bytes, hex_farge: str) -> bytes:
    """Kjernen: leser den transparente master-PDF-en (fra bytes, ikke disk -
    webhook-konteksten har ingen lokal fil å peke til), lager en flat
    fargeside i EKSAKT samme størrelse med reportlab, legger den
    transparente siden OPPÅ (pypdf sin merge_page), og returnerer den
    ferdige, flate PDF-en som bytes - klar til opplasting og til Gelato."""
    leser = PdfReader(io.BytesIO(transparent_pdf_bytes))
    side = leser.pages[0]
    bredde = float(side.mediabox.width)
    hoyde = float(side.mediabox.height)

    r, g, b = _hex_til_rgb01(hex_farge)
    bakgrunn_buf = io.BytesIO()
    c = canvas.Canvas(bakgrunn_buf, pagesize=(bredde, hoyde))
    c.setFillColorRGB(r, g, b)
    c.rect(0, 0, bredde, hoyde, fill=1, stroke=0)
    c.save()
    bakgrunn_buf.seek(0)

    bakgrunn_leser = PdfReader(bakgrunn_buf)
    bakgrunn_side = bakgrunn_leser.pages[0]
    bakgrunn_side.merge_page(side)

    skriver = PdfWriter()
    skriver.add_page(bakgrunn_side)
    ut_buf = io.BytesIO()
    skriver.write(ut_buf)
    return ut_buf.getvalue()
