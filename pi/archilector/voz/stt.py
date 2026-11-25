"""Comandos hablados con Vosk (Alpha Cephei, 2024), sin internet y con el
vocabulario restringido a unas pocas palabras (sección 3.6 del Diseño)."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
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


class Escucha:
    def __init__(self, modelo: str, al_comando: Callable[[str], None], hablando: Callable[[], bool] = lambda: False):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        if not shutil.which("arecord"):
            raise RuntimeError("no se encontró arecord (paquete alsa-utils)")
        gramatica = json.dumps(list(PALABRAS) + ["[unk]"], ensure_ascii=False)
        self._reconocedor = KaldiRecognizer(Model(modelo), FRECUENCIA, gramatica)
        self.al_comando = al_comando
        self.hablando = hablando
        self._activo = threading.Event()
        self._proceso: subprocess.Popen | None = None
        self._hilo: threading.Thread | None = None

    def iniciar(self) -> None:
        self._activo.set()
        self._proceso = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", str(FRECUENCIA), "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE,
        )
        self._hilo = threading.Thread(target=self._escuchar, daemon=True, name="vosk")
        self._hilo.start()
        log.info("Escucha de comandos por voz activa")

    def detener(self) -> None:
        self._activo.clear()
        if self._proceso:
            self._proceso.kill()

    def _escuchar(self) -> None:
        while self._activo.is_set():
            datos = self._proceso.stdout.read(4000)
            if not datos:
                break
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
        return Escucha(cfg_voz["vosk_modelo"], al_comando, hablando)
    except Exception as exc:
        log.warning("Vosk no disponible (%s): solo teclado y panel web", exc)
        return None
