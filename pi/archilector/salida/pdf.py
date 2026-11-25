"""PDF buscable: cada página es la foto con una capa de texto invisible en las
mismas coordenadas que encontró el OCR, de modo que se puede buscar y copiar.

El PDF se arma al terminar el documento (o a pedido desde el panel web) a
partir de las imágenes y los JSON del OCR guardados en disco, así un corte de
energía a mitad del libro no pierde lo ya capturado.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

PPP = 186  # resolución sobre la página con la cámara a 350 mm (tabla 6.2)
FUENTE = "Helvetica"


def _texto_latin1(texto: str) -> str:
    # Las fuentes estándar del PDF usan WinAnsi: cubre el español; lo demás se reemplaza
    return texto.encode("cp1252", "replace").decode("cp1252")


def generar(paginas: list[tuple[str, str | None]], ruta_pdf: str | Path, titulo: str = "ArchiLector",
            ppp: int = PPP) -> Path:
    """``paginas``: lista de (ruta_imagen, ruta_json_ocr o None)."""
    ruta_pdf = Path(ruta_pdf)
    ruta_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(ruta_pdf))
    c.setTitle(titulo)
    c.setCreator("ArchiLector 2.2")
    escala = 72.0 / ppp  # píxeles -> puntos

    for ruta_img, ruta_ocr in paginas:
        with Image.open(ruta_img) as im:
            ancho_px, alto_px = im.size
        ancho_pt, alto_pt = ancho_px * escala, alto_px * escala
        c.setPageSize((ancho_pt, alto_pt))
        c.drawImage(ImageReader(ruta_img), 0, 0, width=ancho_pt, height=alto_pt)

        if ruta_ocr and Path(ruta_ocr).exists():
            datos = json.loads(Path(ruta_ocr).read_text(encoding="utf-8"))
            for linea in datos.get("lineas", []):
                texto = _texto_latin1(linea["texto"]).strip()
                if not texto:
                    continue
                xs = [p[0] for p in linea["caja"]]
                ys = [p[1] for p in linea["caja"]]
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                alto_caja = max(1.0, (y1 - y0) * escala)
                tamano = alto_caja * 0.85
                t = c.beginText()
                t.setTextRenderMode(3)  # invisible
                t.setFont(FUENTE, tamano)
                ancho_natural = c.stringWidth(texto, FUENTE, tamano)
                if ancho_natural > 0:
                    t.setHorizScale(100.0 * (x1 - x0) * escala / ancho_natural)
                # Línea base un poco por encima del borde inferior de la caja
                t.setTextOrigin(x0 * escala, alto_pt - y1 * escala + alto_caja * 0.15)
                t.textLine(texto)
                c.drawText(t)
        c.showPage()
    c.save()
    return ruta_pdf
