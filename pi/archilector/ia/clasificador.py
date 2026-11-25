"""Clasificador de páginas (sección 4.6 del Diseño): texto corrido, formulario o
tabla, portada o índice, o página en blanco.

En modo archivo organiza el PDF; en modo lectura permite saltar las páginas en
blanco y avisar «esta página tiene una tabla». Sin modelo entrenado usa una
regla de tinta, líneas y palabras clave (la línea base de la comparación).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

from .tflite import cargar_si_existe

TEXTO = "texto"
FORMULARIO_TABLA = "formulario_tabla"
PORTADA_INDICE = "portada_indice"
BLANCO = "blanco"
CATEGORIAS = [TEXTO, FORMULARIO_TABLA, PORTADA_INDICE, BLANCO]

NOMBRES = {
    TEXTO: "texto",
    FORMULARIO_TABLA: "formulario o tabla",
    PORTADA_INDICE: "portada o índice",
    BLANCO: "página en blanco",
}

_PALABRAS_FORMULARIO = re.compile(r"\b(nombre|fecha|firma|c\.?i\.?|n[°ºo]\.?|total|cantidad|apellido)s?\s*:", re.I)
_PALABRAS_INDICE = re.compile(r"\b(índice|indice|contenido|sumario|tabla de contenidos?)\b", re.I)
_RENGLON_INDICE = re.compile(r"(\.{3,}|…)\s*\d+\s*$|\s\d{1,3}\s*$")


@dataclass
class Categoria:
    categoria: str
    confianza: float
    fuente: str


def _fraccion_tinta(gris: np.ndarray) -> float:
    binaria = cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    return float(np.mean(binaria > 0))


def _lineas_rectas(gris: np.ndarray) -> int:
    binaria = cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    h, w = gris.shape
    horiz = cv2.morphologyEx(binaria, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (w // 6, 1)))
    vert = cv2.morphologyEx(binaria, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 8)))
    n_h = len(cv2.findContours(horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
    n_v = len(cv2.findContours(vert, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
    return n_h + n_v


def regla(pagina_bgr: np.ndarray, texto: str) -> Categoria:
    gris = cv2.cvtColor(pagina_bgr, cv2.COLOR_BGR2GRAY)
    esc = 600 / max(gris.shape)
    gris = cv2.resize(gris, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
    tinta = _fraccion_tinta(gris)
    renglones = [r for r in texto.splitlines() if r.strip()]
    letras = sum(len(r) for r in renglones)

    if tinta < 0.004 and letras < 10:
        return Categoria(BLANCO, 0.9, "regla")
    if _PALABRAS_INDICE.search(texto) or (
        renglones and sum(bool(_RENGLON_INDICE.search(r)) for r in renglones) > 0.5 * len(renglones) and len(renglones) >= 5
    ):
        return Categoria(PORTADA_INDICE, 0.7, "regla")
    if len(_PALABRAS_FORMULARIO.findall(texto)) >= 2 or _lineas_rectas(gris) >= 8:
        return Categoria(FORMULARIO_TABLA, 0.7, "regla")
    if letras < 120 and renglones:
        return Categoria(PORTADA_INDICE, 0.55, "regla")  # pocas palabras grandes: portada
    return Categoria(TEXTO, 0.7, "regla")


class Clasificador:
    def __init__(self, ruta_modelo: str | None = None):
        self.modelo = cargar_si_existe(ruta_modelo) if ruta_modelo else None
        self.categorias = (self.modelo.etiquetas if self.modelo and self.modelo.etiquetas else CATEGORIAS)

    def clasificar(self, pagina_bgr: np.ndarray, texto: str = "") -> Categoria:
        if self.modelo is None:
            return regla(pagina_bgr, texto)
        rgb = cv2.cvtColor(cv2.resize(pagina_bgr, (self.modelo.ancho, self.modelo.alto)), cv2.COLOR_BGR2RGB)
        p = self.modelo.predecir(rgb)
        i = int(np.argmax(p))
        return Categoria(self.categorias[i], float(p[i]), "modelo")
