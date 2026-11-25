"""Comandos hablados con Vosk (Alpha Cephei, 2024), sin internet y con el
vocabulario restringido a unas pocas palabras (sección 3.6 del Diseño).

El micrófono se elige con ``voz.microfono``: un dispositivo ALSA exacto
(``plughw:CARD=Camera,DEV=0``) o fragmentos del nombre de la tarjeta en orden de
preferencia (``[GW600, USB]``). Si el micrófono deja de entregar audio (se
desconectó la webcam), la escucha vuelve a buscarlo y se relanza sola.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# palabra dicha -> comando del orquestador
PALABRAS = {
    "siguiente": "siguiente",
    "repite": "repetir",
    "repetir": "repetir",
    "pausa": "pausa",
    "continuar": "iniciar",
    "continúa": "iniciar",
    "anterior": "anterior",
    "parar": "parar",
    "ayuda": "ayuda",
}
# Mientras el robot habla solo se aceptan estas (evita que el propio texto leído dispare órdenes)
DURANTE_LECTURA = {"pausa", "parar"}

FRECUENCIA = 16000
ASOUND = Path("/proc/asound")
ESPERA_REINTENTO_S = 3.0

# " 2 [Camera         ]: USB-Audio - USB 2.0 Camera"
_LINEA_TARJETA = re.compile(r"^\s*(\d+)\s+\[([^\]]+?)\s*\]:\s*(.*)$")


def tarjetas_con_captura(asound: Path = ASOUND) -> list[tuple[str, str]]:
    """Tarjetas ALSA que tienen micrófono: ``[(id, descripción)]`` en el orden del sistema."""
    try:
        lineas = (asound / "cards").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    res = []
    for i, linea in enumerate(lineas):
        m = _LINEA_TARJETA.match(linea)
        if not m:
            continue
        numero, ident, desc = m.groups()
        if i + 1 < len(lineas) and not _LINEA_TARJETA.match(lineas[i + 1]):
            desc += " " + lineas[i + 1].strip()  # segunda línea: nombre largo y puerto USB
        if any((asound / f"card{numero}").glob("pcm*c")):  # «c» = captura; la HDMI solo tiene «p»
            res.append((ident, desc))
    return res


def resolver_microfono(preferencias, asound: Path = ASOUND) -> str | None:
    """Dispositivo ALSA para ``arecord -D``, o None para el predeterminado."""
    if not preferencias:
        return None
    if isinstance(preferencias, str):
        preferencias = [preferencias]
    tarjetas = None
    for pref in preferencias:
        pref = str(pref).strip()
        if ":" in pref:  # dispositivo ALSA explícito
            return pref
        if tarjetas is None:
            tarjetas = tarjetas_con_captura(asound)
        for ident, desc in tarjetas:
            if pref.lower() in f"{ident} {desc}".lower():
                return f"plughw:CARD={ident},DEV=0"  # plughw convierte a 16 kHz mono
    log.warning("No se encontró el micrófono %s; se usa el predeterminado", preferencias)
    return None


class Escucha:
    def __init__(self, modelo: str, al_comando: Callable[[str], None], hablando: Callable[[], bool] = lambda: False,
                 microfono=None, reconocedor=None, asound: Path = ASOUND):
        if not shutil.which("arecord"):
            raise RuntimeError("no se encontró arecord (paquete alsa-utils)")
        if reconocedor is None:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            SetLogLevel(-1)
            gramatica = json.dumps(list(PALABRAS) + ["[unk]"], ensure_ascii=False)
            reconocedor = KaldiRecognizer(Model(modelo), FRECUENCIA, gramatica)
        self._reconocedor = reconocedor
        self.al_comando = al_comando
        self.hablando = hablando
        self.preferencias = microfono
        self.asound = asound
        self.microfono_activo: str | None = None  # lo que se resolvió; None = predeterminado
        self.escuchando = False
        self._parar = threading.Event()
        self._proceso: subprocess.Popen | None = None
        self._hilo: threading.Thread | None = None

    @property
    def descripcion_microfono(self) -> str:
        if not self.escuchando:
            return "sin audio"
        return self.microfono_activo or "predeterminado"

    def iniciar(self) -> None:
        self._parar.clear()
        self._hilo = threading.Thread(target=self._supervisar, daemon=True, name="vosk")
        self._hilo.start()

    def detener(self) -> None:
        self._parar.set()
        if self._proceso:
            self._proceso.kill()

    def _supervisar(self) -> None:
        """Lanza arecord y lo vuelve a lanzar si termina (micrófono desconectado o cambiado)."""
        while not self._parar.is_set():
            self.microfono_activo = resolver_microfono(self.preferencias, self.asound)
            orden = ["arecord", "-q", "-f", "S16_LE", "-r", str(FRECUENCIA), "-c", "1", "-t", "raw"]
            if self.microfono_activo:
                orden[1:1] = ["-D", self.microfono_activo]
            try:
                self._proceso = subprocess.Popen(orden, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as exc:
                log.warning("No se pudo lanzar arecord: %s", exc)
            else:
                self.escuchando = True
                log.info("Escucha de comandos por voz activa (micrófono: %s)",
                         self.microfono_activo or "predeterminado")
                try:
                    self._escuchar()
                finally:
                    self.escuchando = False
                    self._proceso.kill()
                    self._proceso.wait()
            if self._parar.is_set():
                return
            log.warning("El micrófono dejó de entregar audio; se reintenta en %.0f s", ESPERA_REINTENTO_S)
            if self._parar.wait(ESPERA_REINTENTO_S):
                return

    def _escuchar(self) -> None:
        while not self._parar.is_set():
            datos = self._proceso.stdout.read(4000)
            if not datos:
                return
            if self._reconocedor.AcceptWaveform(datos):
                texto = json.loads(self._reconocedor.Result()).get("text", "")
                for palabra in texto.split():
                    comando = PALABRAS.get(palabra)
                    if not comando:
                        continue
                    if self.hablando() and comando not in DURANTE_LECTURA:
                        continue
                    log.info("Comando de voz: %s", palabra)
                    self.al_comando(comando)
                    break


def crear(cfg_voz: dict, al_comando, hablando) -> Escucha | None:
    if not (cfg_voz.get("activa", True) and cfg_voz.get("escuchar", True)):
        return None
    try:
        return Escucha(cfg_voz["vosk_modelo"], al_comando, hablando, cfg_voz.get("microfono"))
    except Exception as exc:
        log.warning("Vosk no disponible (%s): solo teclado y panel web", exc)
        return None
