"""Bloque B4 · captura de la página.

Fuentes:
* ``pi``            cámara V1.3 por CSI con Picamera2 (2592 × 1944, prueba T4.1)
* ``sim``           foto del ``LibroSimulado``
* ``carpeta:<dir>`` recorre las imágenes de una carpeta (desarrollo)
* ``webcam:<n>``    cámara USB con OpenCV (desarrollo)
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


class Camara:
    def capturar(self) -> np.ndarray:  # BGR
        raise NotImplementedError

    def cerrar(self) -> None:
        pass


class CamaraPi(Camara):
    def __init__(self, ancho: int = 2592, alto: int = 1944):
        from picamera2 import Picamera2  # solo existe en la Raspberry Pi

        self._cam = Picamera2()
        config = self._cam.create_still_configuration(main={"size": (ancho, alto), "format": "RGB888"})
        self._cam.configure(config)
        self._cam.start()
        self._lock = threading.Lock()

    def capturar(self) -> np.ndarray:
        with self._lock:
            # Con formato RGB888, Picamera2 entrega los bytes en orden BGR (el de OpenCV)
            return self._cam.capture_array("main")

    def cerrar(self) -> None:
        self._cam.stop()
        self._cam.close()


class CamaraSimulada(Camara):
    def __init__(self, libro):
        self.libro = libro

    def capturar(self) -> np.ndarray:
        return self.libro.renderizar()


class CamaraCarpeta(Camara):
    EXTENSIONES = {".jpg", ".jpeg", ".png", ".bmp"}

    def __init__(self, carpeta: str):
        self.archivos = sorted(p for p in Path(carpeta).iterdir() if p.suffix.lower() in self.EXTENSIONES)
        if not self.archivos:
            raise FileNotFoundError(f"no hay imágenes en {carpeta}")
        self.i = 0

    def capturar(self) -> np.ndarray:
        img = cv2.imread(str(self.archivos[self.i % len(self.archivos)]))
        return img

    def avanzar(self) -> None:
        self.i += 1


class CamaraWebcam(Camara):
    def __init__(self, indice: int = 0):
        self._cap = cv2.VideoCapture(indice)

    def capturar(self) -> np.ndarray:
        for _ in range(3):  # descarta cuadros viejos del búfer
            self._cap.read()
        ok, img = self._cap.read()
        if not ok:
            raise RuntimeError("la webcam no entregó imagen")
        return img

    def cerrar(self) -> None:
        self._cap.release()


def crear(fuente: str, libro=None, ancho: int = 2592, alto: int = 1944) -> Camara:
    if fuente == "pi":
        return CamaraPi(ancho, alto)
    if fuente == "sim":
        if libro is None:
            raise ValueError("la cámara simulada necesita un LibroSimulado")
        return CamaraSimulada(libro)
    if fuente.startswith("carpeta:"):
        return CamaraCarpeta(fuente.split(":", 1)[1])
    if fuente.startswith("webcam"):
        _, _, n = fuente.partition(":")
        return CamaraWebcam(int(n or 0))
    raise ValueError(f"fuente de cámara desconocida: {fuente}")
