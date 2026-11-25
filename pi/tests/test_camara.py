"""Elección y cambio de cámara (CSI V1.3 o webcam USB) sin hardware."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from archilector import camara as cam
from archilector import config as configuracion
from archilector.orquestador import ARCHIVO
from archilector.web.app import crear_app
from tests.test_orquestador import VerificadorGuionado, armar


class CamaraFalsa(cam.Camara):
    def __init__(self, fuente, resolucion=(1280, 720)):
        self.fuente = fuente
        self.resolucion = resolucion
        self.cerrada = False

    def capturar(self):
        return np.full((self.resolucion[1], self.resolucion[0], 3), 200, np.uint8)

    def cerrar(self):
        self.cerrada = True


def abridor(fallan=()):
    abiertas = {}

    def abrir(fuente):
        if fuente in fallan:
            raise RuntimeError(f"no se pudo abrir {fuente}")
        abiertas[fuente] = CamaraFalsa(fuente)
        return abiertas[fuente]

    return abrir, abiertas


def test_cambiar_cierra_la_anterior_solo_si_la_nueva_abre(tmp_path):
    abrir, abiertas = abridor(fallan={"webcam:1"})
    vistas = []
    c = cam.CamaraConmutable(abrir, "pi", vistas.append, memoria=tmp_path / "camara.txt")
    assert c.fuente == "pi" and vistas == ["pi"]

    with pytest.raises(RuntimeError):
        c.cambiar("webcam:1")
    assert c.fuente == "pi" and not abiertas["pi"].cerrada

    assert c.cambiar("webcam:0") is True
    assert c.fuente == "webcam:0" and abiertas["pi"].cerrada
    assert c.capturar().shape == (720, 1280, 3)
    assert vistas == ["pi", "webcam:0"]
    assert (tmp_path / "camara.txt").read_text(encoding="utf-8") == "webcam:0"

    assert c.cambiar("webcam:0") is False  # ya era la activa


def test_auto_prioriza_la_recordada_luego_csi_luego_webcam(tmp_path, monkeypatch):
    detectadas = [{"fuente": "pi", "nombre": "CSI"}, {"fuente": "webcam:/dev/v4l/by-id/usb-GW600", "nombre": "GW600"}]
    monkeypatch.setattr(cam, "detectar", lambda no_probar=None: list(detectadas))
    monkeypatch.setattr(cam, "_abrir", lambda fuente, libro, cfgc: CamaraFalsa(fuente))

    assert cam.crear("auto", datos=tmp_path).fuente == "pi"

    (tmp_path / "camara.txt").write_text("webcam:/dev/v4l/by-id/usb-GW600", encoding="utf-8")
    assert cam.crear("auto", datos=tmp_path).fuente == "webcam:/dev/v4l/by-id/usb-GW600"

    (tmp_path / "camara.txt").write_text("webcam:/dev/v4l/by-id/usb-otra", encoding="utf-8")  # ya no está
    assert cam.crear("auto", datos=tmp_path).fuente == "pi"

    detectadas.pop(0)  # sin CSI
    assert cam.crear("auto", datos=tmp_path).fuente == "webcam:/dev/v4l/by-id/usb-GW600"

    detectadas.clear()
    assert cam.crear("auto", libro=object(), datos=tmp_path).fuente == "sim"
    with pytest.raises(RuntimeError):
        cam.crear("auto", datos=tmp_path)


def test_region_del_canto_por_camara():
    cfg = configuracion.cargar(None)
    cfg["camara"]["region_canto"] = {"webcam": [0.05, 0.2, 0.2, 0.8]}
    assert cam.region_canto(cfg, "pi") == cfg["vision"]["region_canto"]
    assert cam.region_canto(cfg, "webcam:/dev/v4l/by-id/usb-GW600") == [0.05, 0.2, 0.2, 0.8]


def test_configuracion_anterior_con_ancho_y_alto():
    cfgc = cam._config_camara({"ancho": 1920, "alto": 1080})
    assert cfgc["pi"] == {"ancho": 1920, "alto": 1080}
    assert cfgc["webcam"]["ancho"] == 1280


def test_panel_cambia_de_camara(tmp_path, monkeypatch):
    monkeypatch.setattr(cam, "detectar", lambda no_probar=None: [{"fuente": "webcam:9", "nombre": "Webcam 9"}])
    o, sim, libro, db = armar(tmp_path, VerificadorGuionado())
    simulada = cam.CamaraSimulada(libro)
    o.camara = cam.CamaraConmutable(lambda f: simulada if f == "sim" else CamaraFalsa(f), "sim",
                                    memoria=tmp_path / "camara.txt", hay_simulada=True)
    cliente = TestClient(crear_app(o, db, o.enlace, o.cfg))

    assert cliente.get("/api/estado").json()["camara"]["fuente"] == "sim"
    pagina = cliente.get("/calibracion").text
    assert "Webcam 9" in pagina and "Libro simulado" in pagina

    r = cliente.post("/camara/fuente", data={"fuente": "webcam:9"}, follow_redirects=False)
    assert r.status_code == 303
    estado = cliente.get("/api/estado").json()["camara"]
    assert estado == {"fuente": "webcam:9", "resolucion": [1280, 720], "conmutable": True}

    foto = cliente.get("/camara/foto.jpg")
    assert foto.status_code == 200 and foto.content[:2] == b"\xff\xd8"

    # Con un documento en curso no se cambia de cámara ni se sacan fotos de prueba
    cliente.post("/camara/fuente", data={"fuente": "sim"})
    o.iniciar_documento("Actas", "actas", ARCHIVO, max_paginas=6)
    try:
        assert cliente.post("/camara/fuente", data={"fuente": "webcam:9"}).status_code == 409
        assert cliente.get("/camara/foto.jpg").status_code == 409
    finally:
        o.esperar_fin(20)
    pares = list((tmp_path / "pares").rglob("meta.json"))
    assert pares and '"camara": "sim"' in pares[0].read_text(encoding="utf-8")
