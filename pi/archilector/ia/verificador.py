"""Verificador de volteo: la IA principal de ArchiLector (Bloque IV del Diseño).

Después de cada volteo mira la foto de antes y la de después y responde cuál
de cuatro cosas pasó. Con esa respuesta el robot sigue, reintenta o se detiene.

Si todavía no hay modelo entrenado (``modelos/verificador.tflite``), usa la
regla de diferencia de píxeles, que solo distingue página nueva de misma página.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import regla_pixeles
from .compuesto import REGION_CANTO, compuesto
from .tflite import cargar_si_existe

log = logging.getLogger(__name__)

PAGINA_NUEVA = "pagina_nueva"
MISMA_PAGINA = "misma_pagina"
DOS_HOJAS = "dos_hojas"
HOJA_DOBLADA = "hoja_doblada"
CLASES = [PAGINA_NUEVA, MISMA_PAGINA, DOS_HOJAS, HOJA_DOBLADA]


@dataclass
class Veredicto:
    clase: str
    confianza: float
    fuente: str  # "modelo" o "regla"
    probabilidades: dict[str, float] = field(default_factory=dict)
    regla: str = ""  # lo que diría la regla de píxeles (para el informe comparativo)
    fraccion_cambio: float = 0.0
    milisegundos: float = 0.0
    imagen_compuesta: np.ndarray | None = field(default=None, repr=False)


class Verificador:
    def __init__(self, ruta_modelo: str | None = None, umbral_regla: float = 0.03, region_canto=REGION_CANTO):
        self.modelo = cargar_si_existe(ruta_modelo) if ruta_modelo else None
        self.umbral_regla = umbral_regla
        self.region_canto = tuple(region_canto)
        self.clases = (self.modelo.etiquetas if self.modelo and self.modelo.etiquetas else CLASES)

    @property
    def fuente(self) -> str:
        return "modelo" if self.modelo else "regla"

    def verificar(self, antes: np.ndarray, despues: np.ndarray) -> Veredicto:
        t0 = time.perf_counter()
        regla = regla_pixeles.evaluar(antes, despues, self.umbral_regla)
        mosaico = compuesto(antes, despues, self.region_canto)
        if self.modelo is None:
            v = Veredicto(regla.clase, regla.confianza, "regla", {regla.clase: regla.confianza},
                          regla.clase, regla.fraccion_cambio)
        else:
            rgb = cv2.cvtColor(cv2.resize(mosaico, (self.modelo.ancho, self.modelo.alto)), cv2.COLOR_BGR2RGB)
            p = self.modelo.predecir(rgb)
            i = int(np.argmax(p))
            v = Veredicto(self.clases[i], float(p[i]), "modelo",
                          {c: float(x) for c, x in zip(self.clases, p)}, regla.clase, regla.fraccion_cambio)
        v.milisegundos = (time.perf_counter() - t0) * 1000
        v.imagen_compuesta = mosaico
        return v
