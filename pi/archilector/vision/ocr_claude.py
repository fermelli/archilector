"""Lectura de la página con Claude (API de Anthropic); el OCR local queda de respaldo.

Se usa si ``ocr.claude.activo`` y hay ``ANTHROPIC_API_KEY`` (en el entorno o en ``pi/.env``).
Sin clave, sin internet o ante cualquier error, la página la lee el motor local.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

import cv2
import numpy as np

from .ocr import LineaOCR, ResultadoOCR, numero_impreso

log = logging.getLogger(__name__)

MODELOS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
PAUSA_S = 120  # tras un error de red no se insiste en cada página
SISTEMA = ("Transcribes fotos de páginas de libros y documentos en español para leerlas en voz alta. "
           "Copia el texto tal cual, en orden de lectura, con acentos, ñ, ¿ y ¡; no corrijas ni inventes. "
           "Devuelve el número de página impreso aparte y no lo incluyas en las líneas. "
           "Si la foto no muestra una página con texto, es_pagina es false.")
ESQUEMA = {
    "type": "object",
    "properties": {
        "es_pagina": {"type": "boolean"},
        "numero_pagina": {"type": ["integer", "null"]},
        "lineas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["es_pagina", "numero_pagina", "lineas"],
    "additionalProperties": False,
}


def _jpeg_b64(img: np.ndarray, lado_max: int = 1568) -> str:
    escala = lado_max / max(img.shape[:2])
    if escala < 1:
        img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    return base64.standard_b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()


def _a_resultado(lineas: list[str], numero, alto: int, ancho: int) -> ResultadoOCR:
    """Claude no da coordenadas: franjas iguales para que el PDF siga siendo buscable."""
    lineas = [t for t in lineas if t.strip()]
    y0, paso = alto * 0.06, alto * 0.88 / max(len(lineas), 1)
    res = [LineaOCR([[ancho * .06, y0 + i * paso], [ancho * .94, y0 + i * paso],
                     [ancho * .94, y0 + (i + 1) * paso], [ancho * .06, y0 + (i + 1) * paso]], t, 0.95)
           for i, t in enumerate(lineas)]
    return ResultadoOCR(res, numero if isinstance(numero, int) else numero_impreso(res, alto))


class OCRClaude:
    def __init__(self, respaldo, modelo: str = MODELOS[0], cliente=None):
        self.respaldo, self.modelo, self._cliente = respaldo, modelo, cliente
        self._pausa_hasta = 0.0
        self.ultimo = "local"  # quién leyó la última página, para el panel

    @property
    def hay_clave(self) -> bool:
        return self._cliente is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))

    def leer(self, pagina: np.ndarray) -> ResultadoOCR:
        if self.hay_clave and time.monotonic() >= self._pausa_hasta:
            try:
                r = self._leer_claude(pagina)
                self.ultimo = self.modelo
                return r
            except Exception as exc:  # sin internet, cuota, rechazo...: lee el OCR local
                log.warning("Claude no leyó la página (%s): se usa el OCR local", exc)
                if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
                    self._pausa_hasta = time.monotonic() + PAUSA_S
        self.ultimo = "local"
        return self.respaldo.leer(pagina)

    def _leer_claude(self, pagina: np.ndarray) -> ResultadoOCR:
        if self._cliente is None:
            import anthropic

            self._cliente = anthropic.Anthropic(timeout=30, max_retries=1)
        m = self._cliente.messages.create(
            model=self.modelo, max_tokens=4096, system=SISTEMA,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ESQUEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(pagina)}},
                {"type": "text", "text": "Transcribe esta página."}]}])
        if m.stop_reason != "end_turn":
            raise RuntimeError(f"respuesta incompleta ({m.stop_reason})")
        d = json.loads(next(b.text for b in m.content if b.type == "text"))
        log.info("Claude (%s) leyó %d líneas; %d+%d tokens", self.modelo, len(d["lineas"]),
                 m.usage.input_tokens, m.usage.output_tokens)
        if not d["es_pagina"]:
            return ResultadoOCR()
        return _a_resultado(d["lineas"], d["numero_pagina"], *pagina.shape[:2])
