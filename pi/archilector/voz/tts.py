"""Lectura en voz alta con Piper (Hansen, 2023), sin internet.

El texto se sintetiza oración por oración en otro hilo, por delante de lo que suena,
y se reproduce con ``aplay`` por el jack de 3,5 mm (amplificador PAM8403). Antes de
empezar se juntan unos segundos de audio para que no haya cortes. Entre oraciones se
revisa si el usuario pidió pausa, repetir o siguiente, para poder cortar la lectura.
"""
from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_ORACIONES = re.compile(r"(?<=[.!?;:])\s+|\n{2,}")
PREVIO_S = 3.0  # audio que se junta antes de empezar a sonar


class _Cortado(Exception):
    pass


def dividir_oraciones(texto: str) -> list[str]:
    return [o.strip() for o in _ORACIONES.split(texto.replace("\n", " ")) if o.strip()]


class Voz:
    """Interfaz común: ``hablar`` bloquea hasta terminar o hasta que ``cortar`` se active."""

    volumen: int = 70
    velocidad: float = 1.0

    def hablar(self, texto: str, cortar: threading.Event | None = None) -> bool:
        raise NotImplementedError

    def detener(self) -> None:
        pass

    def ajustar_volumen(self, delta: int) -> None:
        self.volumen = int(min(100, max(10, self.volumen + delta)))

    def ajustar_velocidad(self, delta: float) -> None:
        # Piper usa length_scale: mayor = más lento
        self.velocidad = float(min(2.0, max(0.5, self.velocidad + delta)))

    @property
    def hablando(self) -> bool:
        return False


class VozConsola(Voz):
    """Respaldo para desarrollo: escribe el texto en el registro."""

    def __init__(self, segundos_por_letra: float = 0.0):
        self.segundos_por_letra = segundos_por_letra
        self.dicho: list[str] = []

    def hablar(self, texto: str, cortar: threading.Event | None = None) -> bool:
        for oracion in dividir_oraciones(texto) or [texto]:
            if cortar is not None and cortar.is_set():
                return False
            log.info("[VOZ] %s", oracion)
            self.dicho.append(oracion)
            time.sleep(len(oracion) * self.segundos_por_letra)
        return True


class VozPiper(Voz):
    def __init__(self, modelo: str | Path, volumen: int = 70, velocidad: float = 1.0):
        from piper import PiperVoice  # paquete piper-tts

        self._voz = PiperVoice.load(str(modelo))
        self.frecuencia = int(self._voz.config.sample_rate)
        self.volumen = volumen
        self.velocidad = velocidad
        self._proceso: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._lock_piper = threading.Lock()
        if not shutil.which("aplay"):
            raise RuntimeError("no se encontró aplay (paquete alsa-utils)")

    @property
    def hablando(self) -> bool:
        return self._proceso is not None and self._proceso.poll() is None

    def _sintetizar(self, oracion: str) -> bytes:
        try:  # piper-tts 1.3 o posterior
            from piper import SynthesisConfig

            cfg = SynthesisConfig(length_scale=self.velocidad)
            return b"".join(ch.audio_int16_bytes for ch in self._voz.synthesize(oracion, syn_config=cfg))
        except ImportError:  # piper-tts 1.2
            return b"".join(self._voz.synthesize_stream_raw(oracion, length_scale=self.velocidad))

    def _abrir_salida(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["aplay", "-q", "-r", str(self.frecuencia), "-f", "S16_LE", "-t", "raw", "-c", "1", "-"],
            stdin=subprocess.PIPE,
        )

    def hablar(self, texto: str, cortar: threading.Event | None = None) -> bool:
        cortado = lambda: cortar is not None and cortar.is_set()  # noqa: E731
        # Piper sintetiza en otro hilo, siempre por delante de lo que suena: si después de una
        # oración corta viene una larga, aplay no se queda sin audio («underrun») a mitad de frase.
        cola: queue.Queue[bytes | None] = queue.Queue()
        fin = threading.Event()

        def producir():
            try:
                for oracion in dividir_oraciones(texto) or [texto]:
                    if fin.is_set() or cortado():
                        break
                    with self._lock_piper:  # si se cortó y empezó otra lectura, no sintetizar dos a la vez
                        audio = np.frombuffer(self._sintetizar(oracion), dtype=np.int16)
                    cola.put((audio.astype(np.float32) * (self.volumen / 100.0)).astype(np.int16).tobytes())
            finally:
                cola.put(None)

        threading.Thread(target=producir, daemon=True, name="piper").start()
        with self._lock:
            self._proceso = self._abrir_salida()
        try:
            # Antes de empezar a sonar se juntan PREVIO_S segundos de audio (o el texto entero si es corto)
            pendiente, juntado, terminado = [], 0, False
            while juntado < PREVIO_S * 2 * self.frecuencia:
                bloque = self._tomar(cola, cortado)
                if bloque is None:
                    terminado = True
                    break
                pendiente.append(bloque)
                juntado += len(bloque)
            while True:
                for bloque in pendiente:
                    self._proceso.stdin.write(bloque)
                self._proceso.stdin.flush()
                if terminado:
                    break
                bloque = self._tomar(cola, cortado)
                if bloque is None:
                    break
                pendiente = [bloque]
            self._proceso.stdin.close()
            # Esperar a que termine de sonar, atento a una interrupción
            while self._proceso.poll() is None:
                if cortar is not None and cortar.is_set():
                    self.detener()
                    return False
                time.sleep(0.05)
            return True
        except _Cortado:
            self.detener()
            return False
        except (BrokenPipeError, OSError):
            return False
        finally:
            fin.set()

    @staticmethod
    def _tomar(cola: queue.Queue, cortado) -> bytes | None:
        while True:
            if cortado():
                raise _Cortado
            try:
                return cola.get(timeout=0.05)
            except queue.Empty:
                continue

    def detener(self) -> None:
        with self._lock:
            if self._proceso and self._proceso.poll() is None:
                self._proceso.kill()


def crear(cfg_voz: dict) -> Voz:
    if not cfg_voz.get("activa", True):
        return VozConsola()
    try:
        return VozPiper(cfg_voz["piper_modelo"], cfg_voz.get("volumen", 70), cfg_voz.get("velocidad", 1.0))
    except Exception as exc:
        log.warning("Piper no disponible (%s): la voz se escribirá en el registro", exc)
        return VozConsola()
