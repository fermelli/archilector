"""Reconocimiento de texto con RapidOCR: los modelos PP-OCR (Du et al., 2020)
ejecutados con onnxruntime, que se instala con pip en la Pi de 64 bits."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class LineaOCR:
    caja: list[list[float]]  # 4 puntos (x, y) en píxeles de la imagen de la página
    texto: str
    confianza: float


@dataclass
class ResultadoOCR:
    lineas: list[LineaOCR] = field(default_factory=list)
    numero_pagina: int | None = None

    @property
    def texto(self) -> str:
        return "\n".join(l.texto for l in self.lineas)

    def a_dict(self) -> dict:
        return {
            "numero_pagina": self.numero_pagina,
            "lineas": [{"caja": l.caja, "texto": l.texto, "confianza": l.confianza} for l in self.lineas],
        }


_SOLO_NUMERO = re.compile(r"^\s*[-–—]?\s*(\d{1,4})\s*[-–—]?\s*$")


def numero_impreso(lineas: list[LineaOCR], alto: int, franja: float = 0.12) -> int | None:
    """Número de página: una línea que solo tiene dígitos, arriba o abajo de la página.
    Sirve como segunda comprobación del verificador (sección 4.2)."""
    candidatos = []
    for l in lineas:
        m = _SOLO_NUMERO.match(l.texto)
        if not m:
            continue
        y = float(np.mean([p[1] for p in l.caja]))
        if y < alto * franja or y > alto * (1 - franja):
            candidatos.append((l.confianza, int(m.group(1))))
    return max(candidatos)[1] if candidatos else None


class OCR:
    def __init__(self):
        self._motor = None

    def _cargar(self):
        if self._motor is None:
            from rapidocr_onnxruntime import RapidOCR

            self._motor = RapidOCR()
        return self._motor

    def leer(self, pagina_bgr: np.ndarray) -> ResultadoOCR:
        resultado, _ = self._cargar()(pagina_bgr)
        lineas = [
            LineaOCR(caja=[[float(x), float(y)] for x, y in caja], texto=str(texto), confianza=float(conf))
            for caja, texto, conf in (resultado or [])
        ]
        # Orden de lectura: de arriba abajo y de izquierda a derecha
        lineas.sort(key=lambda l: (round(l.caja[0][1] / 15), l.caja[0][0]))
        return ResultadoOCR(lineas=lineas, numero_pagina=numero_impreso(lineas, pagina_bgr.shape[0]))


class OCRNulo:
    """Para cuando el OCR está desactivado."""

    def leer(self, pagina_bgr: np.ndarray) -> ResultadoOCR:
        return ResultadoOCR()
