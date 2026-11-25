"""Registro de pares antes/después para entrenar el verificador (sección 4.3, paso 1).

Cada volteo queda en ``datos/pares/<libro>/<fecha>_<n>/`` con ``antes.jpg``,
``despues.jpg``, ``compuesto.jpg`` y ``meta.json`` (parámetros, intento,
predicción y lo que dijo la regla de píxeles). El panel web los muestra para
etiquetarlos.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import cv2
import numpy as np


def _nombre_seguro(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", texto).strip("_") or "libro"


class RegistroPares:
    def __init__(self, carpeta: str | Path, activo: bool = True):
        self.carpeta = Path(carpeta)
        self.activo = activo
        self._n = 0

    def guardar(self, libro: str, antes: np.ndarray, despues: np.ndarray, compuesto: np.ndarray,
                meta: dict) -> Path | None:
        if not self.activo:
            return None
        self._n += 1
        destino = self.carpeta / _nombre_seguro(libro) / f"{time.strftime('%Y%m%d_%H%M%S')}_{self._n:04d}"
        destino.mkdir(parents=True, exist_ok=True)
        # Fotos reducidas a la mitad: suficientes para el mosaico de 224 px y livianas
        for nombre, img in (("antes", antes), ("despues", despues)):
            chica = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(destino / f"{nombre}.jpg"), chica, [cv2.IMWRITE_JPEG_QUALITY, 88])
        cv2.imwrite(str(destino / "compuesto.jpg"), compuesto, [cv2.IMWRITE_JPEG_QUALITY, 95])
        meta = {"libro": libro, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), **meta}
        (destino / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return destino
