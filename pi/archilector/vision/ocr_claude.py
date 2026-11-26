"""Lectura de la página con Claude (API de Anthropic); el OCR local queda de respaldo.

Se usa si ``ocr.claude.activo`` y hay ``ANTHROPIC_API_KEY`` (en el entorno o en ``pi/.env``).
Sin clave, sin internet o ante cualquier error, la página la lee el motor local.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
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
           "Si en un costado asoma un trozo de la página vecina, ignóralo: transcribe solo la página principal. "
           "categoria: texto (texto corrido), formulario_tabla, portada_indice (portada o índice) o blanco. "
           "Si la foto no muestra una página (la sala, una mano), es_pagina es false.")
ESQUEMA = {
    "type": "object",
    "properties": {
        "es_pagina": {"type": "boolean"},
        "numero_pagina": {"type": ["integer", "null"]},
        "lineas": {"type": "array", "items": {"type": "string"}},
        "categoria": {"type": "string", "enum": ["texto", "formulario_tabla", "portada_indice", "blanco"]},
    },
    "required": ["es_pagina", "numero_pagina", "lineas", "categoria"],
    "additionalProperties": False,
}
# Libro abierto entero: Claude ubica el lomo y lee cada página por separado
PLIEGO = ("La foto muestra un libro abierto. lomo: dónde está el pliegue entre las dos páginas, como fracción del "
          "ancho de la foto (0 = borde izquierdo, 1 = borde derecho). izquierda y derecha: cada página por separado; "
          "si de un lado no hay página (la tapa, la mesa), es_pagina es false.")
ESQUEMA_PLIEGO = {
    "type": "object",
    "properties": {"lomo": {"type": "number"}, "izquierda": ESQUEMA, "derecha": ESQUEMA},
    "required": ["lomo", "izquierda", "derecha"],
    "additionalProperties": False,
}


def _jpeg_b64(img: np.ndarray, lado_max: int = 1568) -> str:
    escala = lado_max / max(img.shape[:2])
    if escala < 1:
        img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    return base64.standard_b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()


def _a_resultado(lineas: list[str], numero, alto: int, ancho: int, categoria: str | None = None) -> ResultadoOCR:
    """Claude no da coordenadas: franjas iguales para que el PDF siga siendo buscable."""
    lineas = [t for t in lineas if t.strip()]
    y0, paso = alto * 0.06, alto * 0.88 / max(len(lineas), 1)
    res = [LineaOCR([[ancho * .06, y0 + i * paso], [ancho * .94, y0 + i * paso],
                     [ancho * .94, y0 + (i + 1) * paso], [ancho * .06, y0 + (i + 1) * paso]], t, 0.95)
           for i, t in enumerate(lineas)]
    return ResultadoOCR(res, numero if isinstance(numero, int) else numero_impreso(res, alto), categoria)


def motivo(exc: Exception) -> str:
    """Por qué falló Claude, en palabras para el panel."""
    if "content filtering" in str(exc):
        return "el filtro de contenido de Anthropic bloqueó la respuesta (suele pasar al transcribir libros publicados)"
    if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
        return "sin conexión con la API"
    return f"{type(exc).__name__}: {exc}"[:200]


class OCRClaude:
    def __init__(self, respaldo, modelo: str = MODELOS[0], cliente=None):
        self.respaldo, self.modelo, self._cliente = respaldo, modelo, cliente
        self._pausa_hasta = 0.0
        self._lock = threading.Lock()  # el OCR local no admite dos páginas a la vez
        self.ultimo = "local"  # quién leyó la última página, para el panel
        self.motivo = ""  # por qué falló la última vez

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
                self.motivo = motivo(exc)
                log.warning("Claude no leyó la página (%s): se usa el OCR local", exc)
                if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
                    self._pausa_hasta = time.monotonic() + PAUSA_S
        self.ultimo = "local"
        with self._lock:
            return self.respaldo.leer(pagina)

    def leer_pliego(self, libro: np.ndarray) -> tuple[int, ResultadoOCR, ResultadoOCR] | None:
        """Claude divide el libro abierto y lee las dos páginas en una sola llamada.
        Devuelve (columna del lomo, izquierda, derecha), o None si no se pudo (se divide en local)."""
        if not self.hay_clave or time.monotonic() < self._pausa_hasta:
            return None
        try:
            d = self._pedir(libro, ESQUEMA_PLIEGO, PLIEGO, 8192, 2576)  # 2576 px: cada página con su detalle
        except Exception as exc:
            self.motivo = motivo(exc)
            log.warning("Claude no dividió el libro (%s): se divide en local", exc)
            if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
                self._pausa_hasta = time.monotonic() + PAUSA_S
            return None
        self.ultimo = self.modelo
        alto, ancho = libro.shape[:2]
        col = int(ancho * min(max(float(d["lomo"]), 0.2), 0.8))
        log.info("Claude (%s) ubicó el lomo en %.0f %% del ancho", self.modelo, 100 * col / ancho)
        return col, self._pagina(d["izquierda"], alto, col), self._pagina(d["derecha"], alto, ancho - col)

    @staticmethod
    def _pagina(d: dict, alto: int, ancho: int) -> ResultadoOCR:
        if not d["es_pagina"]:
            return ResultadoOCR()
        return _a_resultado(d["lineas"], d["numero_pagina"], alto, ancho, d.get("categoria"))

    def _leer_claude(self, pagina: np.ndarray) -> ResultadoOCR:
        return self._pagina(self._pedir(pagina, ESQUEMA, "Transcribe esta página."), *pagina.shape[:2])

    def _pedir(self, img: np.ndarray, esquema: dict, pedido: str, max_tokens: int = 4096, lado_max: int = 1568) -> dict:
        if self._cliente is None:
            import anthropic

            self._cliente = anthropic.Anthropic(timeout=60, max_retries=1)
        m = self._cliente.messages.create(
            model=self.modelo, max_tokens=max_tokens, system=SISTEMA,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": esquema}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(img, lado_max)}},
                {"type": "text", "text": pedido}]}])
        if m.stop_reason != "end_turn":
            raise RuntimeError(f"respuesta incompleta ({m.stop_reason})")
        d = json.loads(next(b.text for b in m.content if b.type == "text"))
        log.info("Claude (%s): %d+%d tokens", self.modelo, m.usage.input_tokens, m.usage.output_tokens)
        return d
