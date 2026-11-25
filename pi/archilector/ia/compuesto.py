"""Imagen de entrada del verificador de volteo (sección 4.2 del Diseño).

Combina en una sola imagen de 224 × 224 × 3 (mosaico 2 × 2, en grises):

    ┌──────────────┬──────────────┐
    │ foto antes   │ foto después │
    ├──────────────┼──────────────┤
    │ canto antes  │ diferencia   │
    │ canto después│ |antes-desp| │
    └──────────────┴──────────────┘

El recorte ampliado del canto de las hojas pasadas deja ver su grosor (una hoja
o dos) y la diferencia resalta la esquina doblada o la falta de cambio.

IMPORTANTE: el cuaderno de Colab copia esta misma función. Si se cambia aquí,
hay que cambiarla allá y volver a entrenar.
"""
from __future__ import annotations

import cv2
import numpy as np

TAM = 224
MEDIO = TAM // 2

# Región del canto de las hojas pasadas, en fracciones de la foto: x0, y0, x1, y1.
# Con el atril fijo, se ajusta una vez tras el montaje (config: vision.region_canto).
REGION_CANTO = (0.0, 0.2, 0.15, 0.8)


def _gris(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def recorte(img: np.ndarray, region=REGION_CANTO) -> np.ndarray:
    h, w = img.shape[:2]
    x0, y0, x1, y1 = region
    return img[int(y0 * h): int(y1 * h), int(x0 * w): int(x1 * w)]


def compuesto(antes: np.ndarray, despues: np.ndarray, region=REGION_CANTO) -> np.ndarray:
    a, d = _gris(antes), _gris(despues)
    if a.shape != d.shape:
        d = cv2.resize(d, (a.shape[1], a.shape[0]))
    celda = lambda x: cv2.resize(x, (MEDIO, MEDIO), interpolation=cv2.INTER_AREA)  # noqa: E731

    canto_a = cv2.resize(recorte(a, region), (MEDIO // 2, MEDIO), interpolation=cv2.INTER_AREA)
    canto_d = cv2.resize(recorte(d, region), (MEDIO // 2, MEDIO), interpolation=cv2.INTER_AREA)
    diferencia = cv2.absdiff(cv2.GaussianBlur(a, (5, 5), 0), cv2.GaussianBlur(d, (5, 5), 0))

    arriba = np.hstack([celda(a), celda(d)])
    abajo = np.hstack([np.hstack([canto_a, canto_d]), celda(diferencia)])
    mosaico = np.vstack([arriba, abajo])
    return cv2.cvtColor(mosaico, cv2.COLOR_GRAY2BGR)
