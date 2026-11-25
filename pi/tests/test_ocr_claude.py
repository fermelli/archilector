"""Lectura con Claude y respaldo local, con un cliente falso (sin red ni clave)."""
import base64
import json
from types import SimpleNamespace as NS

import cv2
import numpy as np
from fastapi.testclient import TestClient

from archilector.ia.clasificador import Clasificador
from archilector.vision.ocr import ResultadoOCR
from archilector.vision.ocr_claude import OCRClaude
from archilector.vision.ocr_tesseract import crear
from archilector.web.app import crear_app
from tests.test_orquestador import VerificadorGuionado, armar


class Local:
    def __init__(self):
        self.veces = 0

    def leer(self, pagina):
        self.veces += 1
        return ResultadoOCR()


class ClienteFalso:
    def __init__(self, *respuestas):
        self.respuestas, self.pedidos = list(respuestas), []
        self.messages = self

    def create(self, **kw):
        self.pedidos.append(kw)
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(r))],
                  usage=NS(input_tokens=1500, output_tokens=300))


class APIConnectionError(Exception):  # mismo nombre que la excepción del SDK
    pass


PAGINA = np.full((1944, 2592, 3), 255, np.uint8)


def test_claude_lee_la_pagina():
    c = ClienteFalso({"es_pagina": True, "numero_pagina": 23, "lineas": ["Capítulo I", "", "Había una vez…"],
                      "categoria": "texto"})
    o = OCRClaude(Local(), cliente=c)
    r = o.leer(PAGINA)
    assert r.categoria == "texto"
    assert r.texto == "Capítulo I\nHabía una vez…" and r.numero_pagina == 23
    assert all(0 <= x <= 2592 and 0 <= y <= 1944 for l in r.lineas for x, y in l.caja)
    assert o.ultimo == "claude-haiku-5-5"
    pedido = c.pedidos[0]
    assert pedido["model"] == "claude-haiku-5-5"
    datos = pedido["messages"][0]["content"][0]["source"]["data"]
    img = cv2.imdecode(np.frombuffer(base64.b64decode(datos), np.uint8), cv2.IMREAD_COLOR)
    assert max(img.shape[:2]) == 1568  # se reduce antes de enviar


def test_foto_sin_pagina_no_se_lee():
    o = OCRClaude(Local(), cliente=ClienteFalso({"es_pagina": False, "numero_pagina": None, "lineas": ["sala"]}))
    assert o.leer(PAGINA).texto == ""


def test_sin_clave_lee_el_ocr_local(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    local = Local()
    o = OCRClaude(local)
    o.leer(PAGINA)
    assert local.veces == 1 and o.ultimo == "local"


def test_sin_internet_usa_el_local_y_no_insiste():
    local, c = Local(), ClienteFalso(APIConnectionError("sin red"))
    o = OCRClaude(local, cliente=c)
    o.leer(PAGINA)
    o.leer(PAGINA)
    assert local.veces == 2 and len(c.pedidos) == 1  # la segunda página ya no intenta la API


def test_otro_error_usa_el_local_y_reintenta():
    local, c = Local(), ClienteFalso(ValueError("json"), {"es_pagina": True, "numero_pagina": None, "lineas": ["x"]})
    o = OCRClaude(local, cliente=c)
    o.leer(PAGINA)
    assert o.leer(PAGINA).texto == "x" and local.veces == 1


def test_panel_cambia_el_modelo(tmp_path):
    o, *_, db = armar(tmp_path, VerificadorGuionado())
    o.ocr = OCRClaude(Local(), cliente=ClienteFalso())
    cliente = TestClient(crear_app(o, db, o.enlace, o.cfg))
    assert "Lectura con Claude" in cliente.get("/calibracion").text
    r = cliente.post("/ocr/modelo", data={"modelo": "claude-opus-5-5"}, follow_redirects=False)
    assert r.status_code == 303 and o.ocr.modelo == "claude-opus-5-5"
    assert cliente.post("/ocr/modelo", data={"modelo": "gpt"}).status_code == 400
    assert cliente.get("/api/estado").json()["claude"]["modelo"] == "claude-opus-5-5"


def test_crear_envuelve_el_motor_local():
    assert isinstance(crear({"motor": "rapidocr", "claude": {"activo": True, "modelo": "claude-sonnet-5-5"}}), OCRClaude)
    assert not isinstance(crear({"motor": "rapidocr", "claude": {"activo": False}}), OCRClaude)


def test_el_clasificador_usa_la_categoria_de_claude():
    blanca = np.full((400, 300, 3), 255, np.uint8)
    c = Clasificador(None).clasificar(blanca, "", "formulario_tabla")
    assert (c.categoria, c.fuente) == ("formulario_tabla", "claude")
    assert Clasificador(None).clasificar(blanca, "", "otra").fuente == "regla"  # sin sugerencia válida, la regla
    assert Clasificador(None).clasificar(blanca, "").categoria == "blanco"
