"""Elección del micrófono por nombre y recuperación de la escucha, sin hardware ni Vosk."""
import io
import json
import time

from archilector.voz import stt

# /proc/asound/cards de una Pi 4 con un micrófono USB y una webcam con micrófono
TARJETAS = {
    0: ("Headphones", "bcm2835_headpho - bcm2835 Headphones", "bcm2835 Headphones", ["pcm0p"]),
    1: ("vc4hdmi0", "vc4-hdmi - vc4-hdmi-0", "vc4-hdmi-0", ["pcm0p"]),
    2: ("Device", "USB-Audio - USB PnP Sound Device",
        "C-Media Electronics Inc. USB PnP Sound Device at usb-0000:01:00.0-1.3, full speed", ["pcm0c", "pcm0p"]),
    3: ("GW600", "USB-Audio - Redragon GW600", "Redragon GW600 at usb-0000:01:00.0-1.4, high speed", ["pcm0c"]),
}


def armar_asound(raiz, numeros):
    raiz.mkdir(exist_ok=True)
    lineas = []
    for n in numeros:
        ident, desc, larga, pcms = TARJETAS[n]
        lineas += [f"{n:2d} [{ident:<15}]: {desc}", f"                      {larga}"]
        carpeta = raiz / f"card{n}"
        carpeta.mkdir(exist_ok=True)
        for pcm in pcms:
            (carpeta / pcm).mkdir(exist_ok=True)
    (raiz / "cards").write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return raiz


def test_tarjetas_con_captura_descarta_hdmi(tmp_path):
    asound = armar_asound(tmp_path / "asound", [0, 1, 2, 3])
    assert [t[0] for t in stt.tarjetas_con_captura(asound)] == ["Device", "GW600"]


def test_resolver_respeta_el_orden_de_preferencia(tmp_path):
    asound = armar_asound(tmp_path / "asound", [0, 1, 2, 3])
    assert stt.resolver_microfono(["gw600", "USB"], asound) == "plughw:CARD=GW600,DEV=0"
    assert stt.resolver_microfono("C-Media", asound) == "plughw:CARD=Device,DEV=0"
    assert stt.resolver_microfono(["hdmi"], asound) is None  # sin captura
    assert stt.resolver_microfono("nada", asound) is None
    assert stt.resolver_microfono(None, asound) is None
    assert stt.resolver_microfono(["plughw:CARD=X,DEV=0", "GW600"], asound) == "plughw:CARD=X,DEV=0"


def test_resolver_cae_al_siguiente_si_falta_la_webcam(tmp_path):
    asound = armar_asound(tmp_path / "asound", [0, 1, 2])
    assert stt.resolver_microfono(["GW600", "USB"], asound) == "plughw:CARD=Device,DEV=0"
    assert stt.resolver_microfono(["GW600"], tmp_path / "no_existe") is None


class ReconocedorFalso:
    def AcceptWaveform(self, datos):
        return True

    def Result(self):
        return json.dumps({"text": "por favor siguiente"})


def test_escucha_se_relanza_con_otro_microfono(tmp_path, monkeypatch):
    asound = armar_asound(tmp_path / "asound", [0, 1, 2, 3])
    ordenes = []

    class ArecordFalso:
        def __init__(self, orden, **kw):
            ordenes.append(orden)
            self.stdout = io.BytesIO(b"\0" * 4000)  # un bloque de audio y luego fin (micrófono quitado)
            if len(ordenes) == 1:
                armar_asound_sin_webcam()

        def kill(self):
            pass

        def wait(self):
            return 0

    def armar_asound_sin_webcam():
        (asound / "cards").unlink()
        armar_asound(asound, [0, 1, 2])

    monkeypatch.setattr(stt.shutil, "which", lambda nombre: "/usr/bin/arecord")
    monkeypatch.setattr(stt.subprocess, "Popen", ArecordFalso)
    monkeypatch.setattr(stt, "ESPERA_REINTENTO_S", 0.05)

    comandos = []
    e = stt.Escucha("sin-modelo", comandos.append, microfono=["GW600", "USB"],
                    reconocedor=ReconocedorFalso(), asound=asound)
    e.iniciar()
    fin = time.monotonic() + 5
    while len(ordenes) < 2 and time.monotonic() < fin:
        time.sleep(0.01)
    e.detener()
    e._hilo.join(2)

    assert ordenes[0][1:3] == ["-D", "plughw:CARD=GW600,DEV=0"]
    assert ordenes[1][1:3] == ["-D", "plughw:CARD=Device,DEV=0"]
    assert comandos[:2] == ["siguiente", "siguiente"]
    assert not e._hilo.is_alive()
