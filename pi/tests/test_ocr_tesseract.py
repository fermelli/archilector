"""OCR con Tesseract y OCR híbrido: parser, regla de «hay página», respaldo y una lectura real."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from archilector.vision import ocr_tesseract as ot
from archilector.vision.ocr import OCR, LineaOCR, OCRNulo, ResultadoOCR
from archilector.vision.ocr_tesseract import OCRHibrido, OCRTesseract, parsear_tsv, valorar

CABECERA = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv(palabras):
    """palabras: (bloque, párrafo, línea, texto, conf, izq, arriba, ancho, alto)."""
    filas = [CABECERA, "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t"]
    for n, (b, p, l, texto, conf, x, y, w, h) in enumerate(palabras, 1):
        filas.append(f"5\t1\t{b}\t{p}\t{l}\t{n}\t{x}\t{y}\t{w}\t{h}\t{conf}\t{texto}")
    return "\n".join(filas) + "\n"


def _pagina_larga(conf=90, lineas=4, por_linea=5):
    pal = []
    for i in range(lineas):
        for j in range(por_linea):
            pal.append((1, 1, i + 1, f"palabra{i}{j}", conf, 50 + j * 120, 100 + i * 40, 100, 30))
    return _tsv(pal)


class _TesseractFalso(OCRTesseract):
    """OCRTesseract sin ejecutar el programa: devuelve un ``tsv`` fijo."""

    def __init__(self, tsv: str):
        self.idioma, self.psm, self.timeout = "spa", 6, 1
        self.conf_palabra, self.min_palabras_confiables, self.conf_media_aceptable = 60, 12, 75
        self.ultima_confianza, self.ultimas_palabras = None, 0
        self._tsv = tsv

    def _ejecutar(self, gris):
        return self._tsv


class _RespaldoFalso:
    def __init__(self, texto: str, conf: float):
        self.llamadas = 0
        self._res = ResultadoOCR(lineas=[LineaOCR(caja=[[0, 0], [1, 0], [1, 1], [0, 1]], texto=texto, confianza=conf)])

    def leer(self, pagina):
        self.llamadas += 1
        return self._res


IMG = np.full((600, 800, 3), 255, np.uint8)


# --------------------------------------------------------------------------------------- parser
def test_parsear_tsv_agrupa_palabras_en_lineas_con_caja_y_confianza():
    tsv = _tsv([
        (1, 1, 1, "Estudiad", 90, 100, 50, 80, 20),
        (1, 1, 1, "atentamente", 80, 190, 52, 110, 22),
        (1, 1, 2, "hábitos", 70, 100, 90, 70, 20),
    ])
    lineas, confs = parsear_tsv(tsv)
    assert [l.texto for l in lineas] == ["Estudiad atentamente", "hábitos"]
    assert lineas[0].caja == [[100.0, 50.0], [300.0, 50.0], [300.0, 74.0], [100.0, 74.0]]
    assert lineas[0].confianza == pytest.approx(0.85)
    assert confs == [90.0, 80.0, 70.0]


def test_parsear_tsv_ignora_filas_sin_palabra_o_sin_confianza():
    tsv = _tsv([(1, 1, 1, "hola", 95, 10, 10, 40, 20)]) + "5\t1\t1\t1\t1\t9\t60\t10\t40\t20\t-1\t \n5\t1\t1\t1\t1\t8\t0\t0\t1\t1\t55\t\n"
    lineas, confs = parsear_tsv(tsv)
    assert [l.texto for l in lineas] == ["hola"] and confs == [95.0]


def test_parsear_tsv_vacio():
    assert parsear_tsv("") == ([], [])
    assert parsear_tsv(CABECERA + "\n") == ([], [])


# --------------------------------------------------------------------------------------- regla
def test_valorar_acepta_paginas_reales_y_rechaza_la_sala():
    pagina = [90.0] * 270 + [40.0] * 30            # medido: 87–91 % con confianza ≥ 60
    sala = [30.0] * 10 + [70.0] * 2 + [40.0] * 4   # medido: 2–6 palabras confiables y media 45–65
    assert valorar(pagina)
    assert not valorar(sala)
    assert not valorar([])


def test_valorar_acepta_portada_con_pocas_palabras_seguras():
    assert valorar([92.0, 88.0, 95.0, 90.0])
    assert not valorar([55.0, 40.0, 30.0])  # pocas y dudosas


# --------------------------------------------------------------------------------------- motor
def test_tesseract_devuelve_lineas_y_numero_de_pagina():
    pal = [(1, 1, i + 1, f"texto{i}{j}", 90, 60 + j * 100, 80 + i * 40, 90, 30) for i in range(3) for j in range(5)]
    pal.append((1, 1, 4, "—", 90, 300, 560, 20, 20))
    pal.append((1, 1, 4, "41", 90, 330, 560, 40, 20))
    pal.append((1, 1, 4, "—", 90, 380, 560, 20, 20))
    t = _TesseractFalso(_tsv(pal))
    r = t.leer(IMG)
    assert r.numero_pagina == 41
    assert len(r.lineas) == 4 and r.texto.splitlines()[0].startswith("texto00")
    assert t.ultimas_palabras == 18 and t.ultima_confianza == pytest.approx(0.90)


def test_tesseract_sin_pagina_devuelve_resultado_vacio():
    t = _TesseractFalso(_tsv([(1, 1, 1, "xq", 30, 0, 0, 10, 10), (1, 1, 1, "ñ", 20, 20, 0, 10, 10)]))
    r = t.leer(IMG)
    assert r.lineas == [] and r.texto == "" and r.numero_pagina is None
    assert t.ultimas_palabras == 2


# --------------------------------------------------------------------------------------- híbrido
def test_hibrido_no_consulta_el_respaldo_si_tesseract_esta_seguro():
    resp = _RespaldoFalso("x" * 999, 0.95)
    h = OCRHibrido(_TesseractFalso(_pagina_larga(conf=90)), resp)
    assert h.leer(IMG).texto.startswith("palabra00")
    assert resp.llamadas == 0


def test_hibrido_no_consulta_el_respaldo_con_muy_pocas_palabras():
    resp = _RespaldoFalso("x" * 999, 0.95)
    h = OCRHibrido(_TesseractFalso(_tsv([(1, 1, 1, "xq", 30, 0, 0, 10, 10)])), resp)
    assert h.leer(IMG).texto == ""
    assert resp.llamadas == 0  # una hoja en blanco no debe costar una segunda lectura


def test_hibrido_con_basura_abundante_consulta_pero_no_acepta_una_lectura_dudosa():
    # Caso medido: una foto de la sala dio 91 palabras sueltas con confianza media 32 %
    basura = _pagina_larga(conf=32, lineas=5, por_linea=6)
    dudosa = _RespaldoFalso("ruido " * 40, 0.55)
    h = OCRHibrido(_TesseractFalso(basura), dudosa)
    assert h.leer(IMG).texto == ""   # Tesseract la rechaza y RapidOCR no es fiable: resultado vacío
    assert dudosa.llamadas == 1


def test_hibrido_usa_rapidocr_si_tesseract_duda_y_rapidocr_trae_mas_texto():
    # 20 palabras con confianza media 62: pasa la regla de página pero está bajo el umbral 0,70
    resp = _RespaldoFalso("texto completo " * 40, 0.93)
    h = OCRHibrido(_TesseractFalso(_pagina_larga(conf=62)), resp)
    assert h.leer(IMG).texto.startswith("texto completo")
    assert resp.llamadas == 1


def test_hibrido_conserva_tesseract_si_rapidocr_no_es_mejor():
    poco = _RespaldoFalso("poco", 0.95)
    mala = _RespaldoFalso("texto completo " * 40, 0.55)
    for resp in (poco, mala):
        h = OCRHibrido(_TesseractFalso(_pagina_larga(conf=62)), resp)
        assert h.leer(IMG).texto.startswith("palabra00")
        assert resp.llamadas == 1


# --------------------------------------------------------------------------------------- fábrica
def test_crear_elige_el_motor():
    assert isinstance(ot.crear({"activo": False}), OCRNulo)
    assert isinstance(ot.crear({"motor": "rapidocr"}), OCR)
    with pytest.raises(ValueError):
        ot.crear({"motor": "otro"})


def test_crear_cae_a_rapidocr_si_tesseract_no_esta(monkeypatch):
    monkeypatch.setattr(ot.shutil, "which", lambda nombre: None)
    assert isinstance(ot.crear({"motor": "hibrido"}), OCR)
    assert isinstance(ot.crear({"motor": "tesseract"}), OCR)


# --------------------------------------------------------------------------------------- lectura real
def _fuente():
    for ruta in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(ruta).exists():
            return ruta
    return None


def _tesseract_con_espanol() -> bool:
    if not shutil.which("tesseract"):
        return False
    return "spa" in subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()


@pytest.mark.skipif(not _tesseract_con_espanol() or _fuente() is None,
                    reason="necesita tesseract con el idioma spa y una fuente TTF del sistema")
def test_lectura_real_reconoce_acentos_y_enye():
    from PIL import Image, ImageDraw, ImageFont

    frases = ["La música del niño sonó más fuerte.", "¿Acaso creéis que habrá mañana?",
              "Estudiad atentamente vuestros hábitos de vida.", "El labrador les deja un espacio."]
    img = Image.new("RGB", (1500, 520), "white")
    d = ImageDraw.Draw(img)
    fuente = ImageFont.truetype(_fuente(), 46)
    for i, f in enumerate(frases):
        d.text((40, 30 + i * 110), f, fill="black", font=fuente)
    bgr = np.array(img)[:, :, ::-1].copy()

    r = OCRTesseract().leer(bgr)
    assert r.lineas, "no leyó nada"
    texto = r.texto
    for esperado in ("música", "niño", "sonó", "más", "hábitos"):
        assert esperado in texto, f"falta «{esperado}» en: {texto!r}"
    alto, ancho = bgr.shape[:2]
    for l in r.lineas:
        assert all(0 <= x <= ancho and 0 <= y <= alto for x, y in l.caja)
