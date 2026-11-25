"""Bloque B4 · captura de la página.

Fuentes:
* ``pi``             cámara V1.3 por CSI con Picamera2 (2592 × 1944, prueba T4.1)
* ``webcam[:<n>]``   cámara USB con OpenCV: índice, ruta (``/dev/v4l/by-id/...``) o la primera que haya
* ``sim``            foto del ``LibroSimulado``
* ``carpeta:<dir>``  recorre las imágenes de una carpeta (desarrollo)
* ``auto``           la última elegida en el panel; si no, la CSI; si no, la primera webcam

``crear()`` devuelve siempre un ``CamaraConmutable``: el panel web puede cambiar
de cámara sin reiniciar, y el orquestador no distingue casos.
"""
from __future__ import annotations

import glob
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger(__name__)

EN_LINUX = sys.platform.startswith("linux")
# V4L2 en la Pi; DirectShow en Windows abre más rápido y acepta MJPG
BACKEND = cv2.CAP_V4L2 if EN_LINUX else cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY


class Camara:
    fuente: str = ""
    resolucion: tuple[int, int] | None = None  # (ancho, alto)

    def capturar(self) -> np.ndarray:  # BGR
        raise NotImplementedError

    def cerrar(self) -> None:
        pass


class CamaraPi(Camara):
    fuente = "pi"

    def __init__(self, ancho: int = 2592, alto: int = 1944, numero: int = 0):
        from picamera2 import Picamera2  # solo existe en la Raspberry Pi

        self._cam = Picamera2(numero)
        config = self._cam.create_still_configuration(main={"size": (ancho, alto), "format": "RGB888"})
        self._cam.configure(config)
        self._cam.start()
        self._lock = threading.Lock()
        self.resolucion = (ancho, alto)

    def capturar(self) -> np.ndarray:
        with self._lock:
            # Con formato RGB888, Picamera2 entrega los bytes en orden BGR (el de OpenCV)
            return self._cam.capture_array("main")

    def cerrar(self) -> None:
        self._cam.stop()
        self._cam.close()


class CamaraSimulada(Camara):
    fuente = "sim"

    def __init__(self, libro):
        self.libro = libro

    def capturar(self) -> np.ndarray:
        return self.libro.renderizar()


class CamaraCarpeta(Camara):
    EXTENSIONES = {".jpg", ".jpeg", ".png", ".bmp"}

    def __init__(self, carpeta: str):
        self.fuente = f"carpeta:{carpeta}"
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
    """Webcam USB (UVC). Pide MJPG para llegar a la resolución máxima por USB 2.0."""

    CALENTAR_S = 1.0  # autoexposición y balance de blancos al abrir

    def __init__(self, dispositivo: int | str = 0, ancho: int = 1280, alto: int = 720, asentar_s: float = 0.5):
        disp = int(dispositivo) if str(dispositivo).isdigit() else str(dispositivo)
        self.fuente = f"webcam:{disp}"
        self.asentar_s = asentar_s
        self._cap = cv2.VideoCapture(disp, BACKEND)
        if not self._cap.isOpened():
            raise RuntimeError(f"no se pudo abrir la webcam {disp}")
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, ancho)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, alto)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        img = self._leer_durante(self.CALENTAR_S)
        if img is None:
            self._cap.release()
            raise RuntimeError(f"la webcam {disp} no entregó imagen")
        self.resolucion = (img.shape[1], img.shape[0])
        if self.resolucion != (ancho, alto):
            log.warning("La webcam %s entrega %d×%d (se pidió %d×%d)", disp, *self.resolucion, ancho, alto)
        log.info("Webcam %s a %d×%d", disp, *self.resolucion)

    def _leer_durante(self, segundos: float) -> np.ndarray | None:
        """Lee cuadros durante ``segundos`` y devuelve el último bueno: vacía el búfer
        y deja que la exposición siga a la luz LED, que acaba de subir al 100 %."""
        img = None
        fin = time.monotonic() + segundos
        limite = fin + 2.0
        while True:
            ok, cuadro = self._cap.read()
            if ok:
                img = cuadro
            ahora = time.monotonic()
            if (ahora >= fin and img is not None) or ahora >= limite:
                return img

    def capturar(self) -> np.ndarray:
        img = self._leer_durante(self.asentar_s)
        if img is None:
            raise RuntimeError("la webcam no entregó imagen")
        return img

    def cerrar(self) -> None:
        self._cap.release()


# =============================================================================
# Detección
# =============================================================================
def _camaras_csi() -> list[tuple[int, str]]:
    """Cámaras de libcamera que no son USB: (número para Picamera2, modelo)."""
    try:
        from picamera2 import Picamera2
        info = Picamera2.global_camera_info()
    except Exception:
        return []
    return [(i, c.get("Model", "CSI")) for i, c in enumerate(info) if "usb" not in str(c.get("Id", "")).lower()]


def _webcams(no_probar: str | None = None) -> list[dict]:
    if EN_LINUX:
        # En la Pi, /dev/video0 es la CSI (unicam) y /dev/video1x el ISP y el códec:
        # solo los enlaces by-id de USB son webcams.
        rutas = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
        return [{"fuente": f"webcam:{r}",
                 "nombre": "Webcam " + Path(r).name.removeprefix("usb-").removesuffix("-video-index0")}
                for r in rutas]
    res = []
    for i in range(3):
        fuente = f"webcam:{i}"
        if fuente != no_probar:  # la que está en uso no se vuelve a abrir
            cap = cv2.VideoCapture(i, BACKEND)
            abierta = cap.isOpened()
            cap.release()
            if not abierta:
                continue
        res.append({"fuente": fuente, "nombre": f"Webcam {i}"})
    return res


def detectar(no_probar: str | None = None) -> list[dict]:
    """Cámaras conectadas: ``[{"fuente": ..., "nombre": ...}]``, la CSI primero."""
    res = [{"fuente": "pi", "nombre": f"Cámara CSI ({modelo})"} for _, modelo in _camaras_csi()[:1]]
    return res + _webcams(no_probar)


# =============================================================================
# Cámara conmutable
# =============================================================================
class CamaraConmutable(Camara):
    """Envuelve la cámara activa y permite cambiarla en caliente."""

    def __init__(self, abrir: Callable[[str], Camara], fuente: str,
                 al_cambiar: Callable[[str], None] | None = None,
                 memoria: Path | None = None, hay_simulada: bool = False):
        self._abrir = abrir
        self._lock = threading.Lock()
        self.al_cambiar = al_cambiar
        self.memoria = memoria
        self.hay_simulada = hay_simulada
        self.activa = abrir(fuente)
        if al_cambiar:
            al_cambiar(self.activa.fuente)

    @property
    def fuente(self) -> str:
        return self.activa.fuente

    @property
    def resolucion(self):
        return self.activa.resolucion

    def capturar(self) -> np.ndarray:
        with self._lock:
            return self.activa.capturar()

    def cambiar(self, fuente: str) -> bool:
        """Abre la nueva antes de cerrar la anterior: si no abre, sigue la anterior
        y se propaga el error. Devuelve False si ya era la activa."""
        with self._lock:
            if fuente == self.activa.fuente:
                return False
            nueva = self._abrir(fuente)
            vieja, self.activa = self.activa, nueva
        try:
            vieja.cerrar()
        except Exception:
            log.exception("No se pudo cerrar la cámara %s", vieja.fuente)
        log.info("Cámara: %s → %s", vieja.fuente, nueva.fuente)
        if self.memoria is not None:
            try:
                self.memoria.write_text(nueva.fuente, encoding="utf-8")
            except OSError as exc:
                log.warning("No se pudo recordar la cámara elegida: %s", exc)
        if self.al_cambiar:
            self.al_cambiar(nueva.fuente)
        return True

    def opciones(self) -> list[dict]:
        """Cámaras que se pueden elegir en el panel (la activa siempre aparece)."""
        res = detectar(no_probar=self.fuente)
        if self.hay_simulada:
            res.append({"fuente": "sim", "nombre": "Libro simulado"})
        if not any(o["fuente"] == self.fuente for o in res):
            res.insert(0, {"fuente": self.fuente, "nombre": self.fuente})
        return res

    def cerrar(self) -> None:
        self.activa.cerrar()

    def __getattr__(self, nombre):  # p. ej. CamaraCarpeta.avanzar
        activa = self.__dict__.get("activa")
        if activa is None:
            raise AttributeError(nombre)
        return getattr(activa, nombre)


def _abrir(fuente: str, libro, cfgc: dict) -> Camara:
    if fuente == "pi":
        csi = _camaras_csi()
        p = cfgc["pi"]
        return CamaraPi(p["ancho"], p["alto"], csi[0][0] if csi else 0)
    if fuente == "sim":
        if libro is None:
            raise ValueError("la cámara simulada necesita un LibroSimulado")
        return CamaraSimulada(libro)
    if fuente.startswith("carpeta:"):
        return CamaraCarpeta(fuente.split(":", 1)[1])
    if fuente == "webcam" or fuente.startswith("webcam:"):
        _, _, disp = fuente.partition(":")
        if not disp:
            hay = _webcams()
            if not hay:
                raise RuntimeError("no se encontró ninguna webcam")
            disp = hay[0]["fuente"].partition(":")[2]
        w = cfgc["webcam"]
        return CamaraWebcam(disp, w["ancho"], w["alto"], w["asentar_s"])
    raise ValueError(f"fuente de cámara desconocida: {fuente}")


def elegir_auto(memoria: Path | None, hay_simulada: bool) -> str:
    """Última elegida en el panel (si sigue conectada) → CSI → primera webcam → simulada."""
    disponibles = [d["fuente"] for d in detectar()]
    if memoria is not None and memoria.exists():
        recordada = memoria.read_text(encoding="utf-8").strip()
        if recordada in disponibles or (recordada == "sim" and hay_simulada):
            return recordada
    if disponibles:
        return disponibles[0]
    if hay_simulada:
        return "sim"
    raise RuntimeError("no se encontró ninguna cámara (ni CSI ni webcam)")


def _config_camara(cfgc: dict | None) -> dict:
    cfgc = dict(cfgc or {})
    pi = {"ancho": 2592, "alto": 1944, **cfgc.get("pi", {})}
    # Compatibilidad: camara.ancho / camara.alto de un config.yaml anterior
    pi.update({k: cfgc[k] for k in ("ancho", "alto") if k in cfgc})
    webcam = {"ancho": 1280, "alto": 720, "asentar_s": 0.5, **cfgc.get("webcam", {})}
    return {**cfgc, "pi": pi, "webcam": webcam}


def crear(fuente: str, libro=None, cfg_camara: dict | None = None, datos: str | Path | None = None,
          al_cambiar: Callable[[str], None] | None = None) -> CamaraConmutable:
    cfgc = _config_camara(cfg_camara)
    memoria = Path(datos) / "camara.txt" if datos is not None else None
    if fuente == "auto":
        fuente = elegir_auto(memoria, libro is not None)
        log.info("Cámara elegida automáticamente: %s", fuente)
    return CamaraConmutable(lambda f: _abrir(f, libro, cfgc), fuente, al_cambiar, memoria, libro is not None)


def region_canto(cfg: dict, fuente: str) -> list[float]:
    """Región del canto para esa cámara: ``camara.region_canto`` por fuente exacta o por tipo
    (``pi``, ``webcam``), y si no hay, ``vision.region_canto``."""
    por_camara = cfg["camara"].get("region_canto") or {}
    tipo = fuente.split(":", 1)[0]
    return list(por_camara.get(fuente) or por_camara.get(tipo) or cfg["vision"]["region_canto"])
