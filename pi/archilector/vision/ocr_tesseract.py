"""Bloque B4 · OCR con Tesseract y OCR híbrido (Tesseract con RapidOCR de respaldo).

Medido en una Raspberry Pi 4 con la foto de una página real (1425 × 1944 px):

* Tesseract ``spa``: 3 a 6 s por página, reconoce acentos, ñ y ¿ ¡. Se confunde más con sombras,
  fondos y páginas inclinadas, y si la imagen no tiene página puede «leer» palabras sueltas.
* RapidOCR (PP-OCRv4 «ch»): 22 a 31 s por página y no reconoce acentos, pero leyó todas las líneas
  incluso con la cámara inclinada.

Por eso el motor recomendado es ``hibrido``: Tesseract primero y RapidOCR solo cuando Tesseract duda
de una página que sí parece tener texto. Los umbrales salen de pocas fotos de un solo libro
(ver ``valorar``): son valores iniciales que conviene recalibrar con las páginas reales del robot.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

from .ocr import OCR, LineaOCR, OCRNulo, ResultadoOCR, numero_impreso

log = logging.getLogger(__name__)


def normalizar_luz(gris: np.ndarray) -> np.ndarray:
    """Divide la imagen por su fondo estimado: quita el degradado de luz y las sombras suaves."""
    fondo = cv2.medianBlur(cv2.dilate(gris, np.ones((25, 25), np.uint8)), 51)
    return cv2.divide(gris, fondo, scale=255)


def parsear_tsv(tsv: str) -> tuple[list[LineaOCR], list[float]]:
    """Convierte la salida ``tsv`` de Tesseract en líneas con su caja y la confianza de cada palabra.

    Devuelve ``(lineas, confianzas_de_palabras)``; las confianzas van de 0 a 100. La confianza de
    cada línea es el promedio de sus palabras, normalizado a 0–1 como la de RapidOCR.
    """
    grupos: dict[tuple[str, str, str], dict] = {}
    confs: list[float] = []
    for fila in tsv.splitlines()[1:]:
        c = fila.split("\t", 11)
        if len(c) < 12 or c[0] != "5":  # nivel 5 = palabra
            continue
        texto = c[11].strip()
        if not texto:
            continue
        try:
            izq, arr, ancho, alto, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        g = grupos.setdefault((c[2], c[3], c[4]), {"pal": [], "conf": [], "x0": izq, "y0": arr, "x1": izq, "y1": arr})
        g["pal"].append(texto)
        g["conf"].append(conf)
        g["x0"], g["y0"] = min(g["x0"], izq), min(g["y0"], arr)
        g["x1"], g["y1"] = max(g["x1"], izq + ancho), max(g["y1"], arr + alto)
        confs.append(conf)
    lineas = []
    for g in grupos.values():  # el orden de aparición es el orden de lectura de Tesseract
        x0, y0, x1, y1 = (float(g[k]) for k in ("x0", "y0", "x1", "y1"))
        lineas.append(LineaOCR(caja=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], texto=" ".join(g["pal"]),
                               confianza=float(np.mean(g["conf"])) / 100.0))
    return lineas, confs


def valorar(confs: list[float], conf_palabra: float = 60, min_confiables: int = 12,
            conf_media_aceptable: float = 75) -> bool:
    """¿La imagen tiene una página legible? Evita «leer» una sala, una mano o una sombra.

    Se acepta si hay al menos ``min_confiables`` palabras con confianza ≥ ``conf_palabra``, o si hay
    pocas palabras pero muy seguras (una portada con unas cuantas palabras grandes).
    Medido: páginas reales 294–314 palabras, 87–91 % con confianza ≥ 60 y media 82–87;
    fotos de la sala 9–16 palabras, 2–6 con confianza ≥ 60 y media 45–65.
    """
    if not confs:
        return False
    confiables = sum(c >= conf_palabra for c in confs)
    media = sum(confs) / len(confs)
    return confiables >= min_confiables or (media >= conf_media_aceptable and confiables >= 1)


class OCRTesseract:
    """Misma interfaz que ``OCR``: ``leer(pagina_bgr) -> ResultadoOCR`` (con cajas para el PDF buscable)."""

    def __init__(self, idioma: str = "spa", psm: int = 6, conf_palabra: float = 60, min_palabras_confiables: int = 12,
                 conf_media_aceptable: float = 75, timeout: float = 120):
        if not shutil.which("tesseract"):
            raise RuntimeError("no se encontró tesseract (sudo apt install tesseract-ocr tesseract-ocr-spa)")
        idiomas = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
        if idioma not in idiomas:
            raise RuntimeError(f"a Tesseract le falta el idioma «{idioma}» (sudo apt install tesseract-ocr-{idioma})")
        self.idioma, self.psm, self.timeout = idioma, psm, timeout
        self.conf_palabra = conf_palabra
        self.min_palabras_confiables = min_palabras_confiables
        self.conf_media_aceptable = conf_media_aceptable
        # De la última lectura, para que el OCR híbrido decida si pide una segunda opinión
        self.ultima_confianza: float | None = None  # 0–1, promedio de las palabras
        self.ultimas_palabras = 0

    def _ejecutar(self, gris: np.ndarray) -> str:
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            cv2.imwrite(f.name, gris)
            r = subprocess.run(["tesseract", f.name, "stdout", "-l", self.idioma, "--psm", str(self.psm), "tsv"],
                               capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(f"tesseract falló: {r.stderr.strip()[:200]}")
        return r.stdout

    def leer(self, pagina_bgr: np.ndarray) -> ResultadoOCR:
        gris = cv2.cvtColor(pagina_bgr, cv2.COLOR_BGR2GRAY) if pagina_bgr.ndim == 3 else pagina_bgr
        lineas, confs = parsear_tsv(self._ejecutar(normalizar_luz(gris)))
        self.ultimas_palabras = len(confs)
        self.ultima_confianza = float(np.mean(confs)) / 100.0 if confs else 0.0
        if not valorar(confs, self.conf_palabra, self.min_palabras_confiables, self.conf_media_aceptable):
            log.info("Tesseract: no hay una página legible (%d palabras, confianza media %.0f)",
                     len(confs), self.ultima_confianza * 100)
            return ResultadoOCR()
        return ResultadoOCR(lineas=lineas, numero_pagina=numero_impreso(lineas, gris.shape[0]))


def _confianza_media(res: ResultadoOCR) -> float:
    return float(np.mean([l.confianza for l in res.lineas])) if res.lineas else 0.0


class OCRHibrido:
    """Tesseract primero; si duda de una página con texto, pide una segunda opinión a RapidOCR.

    Duda = al menos ``min_palabras`` palabras leídas pero con confianza media < ``umbral_respaldo``,
    aunque Tesseract haya concluido que no hay página legible (podría ser una página difícil).
    Se usa el resultado de RapidOCR solo si trae claramente más texto (> 20 %) y sus líneas tienen
    confianza media ≥ 0,80; si no, se queda el de Tesseract. Una foto sin página (medido: 91 palabras
    sueltas con confianza 32 %) consulta a RapidOCR, que no encuentra texto fiable, y el resultado sigue
    vacío; ese intento cuesta ~2,5 s en una imagen chica y hasta ~25 s a 2592×1944.
    """

    def __init__(self, principal: OCRTesseract, respaldo, umbral_respaldo: float = 0.70, min_palabras: int = 12):
        self.principal, self.respaldo = principal, respaldo
        self.umbral_respaldo, self.min_palabras = umbral_respaldo, min_palabras

    def leer(self, pagina_bgr: np.ndarray) -> ResultadoOCR:
        r = self.principal.leer(pagina_bgr)
        conf, n = self.principal.ultima_confianza, self.principal.ultimas_palabras
        if not (n >= self.min_palabras and conf is not None and conf < self.umbral_respaldo):
            return r
        log.info("Tesseract duda (%d palabras, confianza %.0f %%): se pide una segunda opinión a RapidOCR", n, conf * 100)
        alterno = self.respaldo.leer(pagina_bgr)
        if len(alterno.texto) > 1.2 * len(r.texto) and _confianza_media(alterno) >= 0.80:
            return alterno
        return r


def crear(cfg_ocr: dict):
    """Elige el motor según ``ocr.motor``; con ``ocr.claude.activo`` lo envuelve: Claude lee y el local respalda."""
    local = _crear_local(cfg_ocr)
    claude = cfg_ocr.get("claude") or {}
    if claude.get("activo") and cfg_ocr.get("activo", True):
        from .ocr_claude import MODELOS, OCRClaude

        return OCRClaude(local, claude.get("modelo") or MODELOS[0])
    return local


def _crear_local(cfg_ocr: dict):
    if not cfg_ocr.get("activo", True):
        return OCRNulo()
    motor = cfg_ocr.get("motor", "rapidocr")
    if motor == "rapidocr":
        return OCR()
    if motor not in ("tesseract", "hibrido"):
        raise ValueError(f"ocr.motor desconocido: {motor!r} (rapidocr, tesseract o hibrido)")
    try:
        t = OCRTesseract(cfg_ocr.get("idioma", "spa"), cfg_ocr.get("psm", 6), cfg_ocr.get("conf_palabra", 60),
                         cfg_ocr.get("min_palabras_confiables", 12), cfg_ocr.get("conf_media_aceptable", 75))
    except RuntimeError as exc:
        log.warning("Tesseract no disponible (%s): se usa RapidOCR", exc)
        return OCR()
    if motor == "tesseract":
        return t
    return OCRHibrido(t, OCR(), cfg_ocr.get("umbral_respaldo", 0.70), cfg_ocr.get("min_palabras_confiables", 12))
