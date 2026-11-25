"""Ejecución de modelos TensorFlow Lite en la Pi, sin internet (sección 4.3, paso 8).

Busca el intérprete en este orden: ``ai_edge_litert`` (nombre nuevo de TFLite),
``tflite_runtime`` y por último ``tensorflow``. Acepta modelos con entrada
float32 (píxeles 0–255; el modelo incluye su propio reescalado) o cuantizada
uint8/int8.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def _interprete(ruta: str):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:
            from tensorflow.lite import Interpreter  # type: ignore
    return Interpreter(model_path=ruta, num_threads=4)


class ModeloTFLite:
    def __init__(self, ruta: str | Path):
        self.ruta = str(ruta)
        self.interp = _interprete(self.ruta)
        self.interp.allocate_tensors()
        self.entrada = self.interp.get_input_details()[0]
        self.salida = self.interp.get_output_details()[0]
        _, self.alto, self.ancho, _ = self.entrada["shape"]
        etiquetas = Path(self.ruta).with_suffix(".txt")
        self.etiquetas = etiquetas.read_text(encoding="utf-8").split() if etiquetas.exists() else None

    def predecir(self, imagen_rgb: np.ndarray) -> np.ndarray:
        """``imagen_rgb``: uint8 de alto × ancho × 3. Devuelve probabilidades."""
        x = imagen_rgb.astype(np.float32)[None, ...]
        tipo = self.entrada["dtype"]
        if tipo in (np.uint8, np.int8):
            escala, cero = self.entrada["quantization"]
            # El reescalado a [-1, 1] está dentro del modelo: se cuantizan los píxeles crudos
            x = np.clip(np.round(x / escala + cero), np.iinfo(tipo).min, np.iinfo(tipo).max)
        self.interp.set_tensor(self.entrada["index"], x.astype(tipo))
        self.interp.invoke()
        y = self.interp.get_tensor(self.salida["index"])[0].astype(np.float32)
        if self.salida["dtype"] in (np.uint8, np.int8):
            escala, cero = self.salida["quantization"]
            y = (y - cero) * escala
        if y.min() < 0 or abs(y.sum() - 1) > 0.05:  # logits -> softmax
            e = np.exp(y - y.max())
            y = e / e.sum()
        return y


def cargar_si_existe(ruta: str | Path) -> ModeloTFLite | None:
    if not ruta or not Path(ruta).exists():
        log.warning("No se encontró el modelo %s: se usará la regla de respaldo", ruta)
        return None
    try:
        modelo = ModeloTFLite(ruta)
        log.info("Modelo cargado: %s", ruta)
        return modelo
    except Exception:
        log.exception("No se pudo cargar %s: se usará la regla de respaldo", ruta)
        return None
