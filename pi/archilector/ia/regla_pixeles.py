"""Regla sin IA (sección 4.2 del Diseño): si la foto de después es muy distinta
de la de antes, la hoja pasó. Es la línea base con la que se compara el
verificador y el respaldo cuando todavía no hay modelo entrenado.

Falla justo en los casos que importan: dos hojas o una hoja doblada también
cambian mucho la imagen, así que la regla los da por buenos.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class ResultadoRegla:
    clase: str            # "pagina_nueva" o "misma_pagina"
    fraccion_cambio: float
    confianza: float


def fraccion_cambio(antes: np.ndarray, despues: np.ndarray, ancho: int = 320, umbral_pixel: int = 25) -> float:
    def preparar(img):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        esc = ancho / g.shape[1]
        g = cv2.resize(g, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(g, (5, 5), 0)

    a, d = preparar(antes), preparar(despues)
    if a.shape != d.shape:
        d = cv2.resize(d, (a.shape[1], a.shape[0]))
    return float(np.mean(cv2.absdiff(a, d) > umbral_pixel))


def evaluar(antes: np.ndarray, despues: np.ndarray, umbral: float = 0.03) -> ResultadoRegla:
    f = fraccion_cambio(antes, despues)
    clase = "pagina_nueva" if f >= umbral else "misma_pagina"
    # Confianza: qué tan lejos del umbral quedó (0,5 en el umbral, 1 lejos)
    distancia = abs(f - umbral) / max(umbral, 1e-6)
    return ResultadoRegla(clase, f, float(min(1.0, 0.5 + 0.5 * distancia)))
