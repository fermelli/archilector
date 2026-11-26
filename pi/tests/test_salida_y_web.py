"""PDF buscable, visión, compuesto del verificador y panel web."""
import io
import json
import zipfile

import cv2
import numpy as np
from fastapi.testclient import TestClient

from archilector.ia.compuesto import TAM, compuesto
from archilector.ia.regla_pixeles import evaluar
from archilector.orquestador import ARCHIVO
from archilector.salida import pdf
from archilector.simulacion import LibroSimulado
from archilector.vision import preproceso
from archilector.vision.ocr import LineaOCR, numero_impreso
from archilector.web.app import crear_app
from tests.test_orquestador import VerificadorGuionado, armar


def test_pdf_buscable(tmp_path):
    img = np.full((800, 600, 3), 255, np.uint8)
    cv2.putText(img, "Hola mundo", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    ruta_img = tmp_path / "p.jpg"
    cv2.imwrite(str(ruta_img), img)
    ruta_ocr = tmp_path / "p.json"
    ruta_ocr.write_text(json.dumps({"lineas": [
        {"caja": [[50, 70], [290, 70], [290, 110], [50, 110]], "texto": "Hola mundo, año 1998", "confianza": 0.98}
    ]}), encoding="utf-8")
    salida = pdf.generar([(str(ruta_img), str(ruta_ocr))] * 2, tmp_path / "d.pdf", titulo="Prueba")
    contenido = salida.read_bytes()
    assert contenido.startswith(b"%PDF") and contenido.count(b"/Type /Page\n") + contenido.count(b"/Type /Page ") >= 2


def test_preproceso_endereza_y_separa_la_pagina():
    foto = LibroSimulado().renderizar()
    r = preproceso.corregir(foto)
    assert r.contorno_hallado
    assert r.pagina.shape[1] < foto.shape[1] * 0.6
    assert r.nitidez > 10


def test_compuesto_y_regla():
    libro = LibroSimulado()
    a = libro.renderizar()
    libro.voltear()
    d = libro.renderizar()
    assert compuesto(a, d).shape == (TAM, TAM, 3)
    assert evaluar(a, a).clase == "misma_pagina"
    assert evaluar(a, d).clase == "pagina_nueva"


def test_numero_impreso_abajo_de_la_pagina():
    lineas = [LineaOCR([[0, 10], [9, 10], [9, 20], [0, 20]], "Capítulo 1", 0.9),
              LineaOCR([[100, 960], [130, 960], [130, 990], [100, 990]], "- 23 -", 0.95)]
    assert numero_impreso(lineas, alto=1000) == 23


def test_panel_web(tmp_path):
    o, sim, libro, db = armar(tmp_path, VerificadorGuionado())
    doc = o.iniciar_documento("Actas", "actas", ARCHIVO, max_paginas=2)
    o.esperar_fin(20)
    cliente = TestClient(crear_app(o, db, o.enlace, o.cfg))

    assert cliente.get("/").status_code == 200
    estado = cliente.get("/api/estado").json()
    assert estado["estado"] == "terminado" and estado["ultima_pagina"].startswith("/datos/")
    assert cliente.get(estado["ultima_pagina"]).status_code == 200

    assert "Actas" in cliente.get("/documentos").text
    assert cliente.get(f"/documentos/{doc}").status_code == 200
    r = cliente.get(f"/documentos/{doc}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")

    # Etiquetado y exportación para Colab
    par = db.par_sin_etiqueta()
    assert cliente.get("/etiquetar").status_code == 200
    cliente.post(f"/etiquetar/{par['id']}", data={"etiqueta": "pagina_nueva"})
    z = zipfile.ZipFile(io.BytesIO(cliente.get("/etiquetar/verificador.zip").content))
    assert any(n.startswith("pagina_nueva/actas__") for n in z.namelist())
    assert "meta.csv" in z.namelist()

    pagina = db.paginas(doc)[0]
    cliente.post(f"/paginas/{pagina['id']}/categoria", data={"categoria": "texto", "doc_id": doc})
    z = zipfile.ZipFile(io.BytesIO(cliente.get("/etiquetar/clasificador.zip").content))
    assert any(n.startswith("texto/") for n in z.namelist())

    # Calibración
    assert cliente.get("/calibracion").status_code == 200
    r = cliente.post("/calibracion/cfg", data={"parametro": "PASOS_ARCO", "valor": 1100}, follow_redirects=False)
    assert r.status_code == 303 and sim.cal["PASOS_ARCO"] == 1100
    cliente.post("/calibracion/comando", data={"comando": "LIGHT", "argumento": "50"})
    assert sim.luz == 50

    # Control manual: cada pieza por separado, con respuesta JSON
    assert "Control manual" in cliente.get("/manual").text
    r = cliente.post("/manual/comando", data={"comando": "MOVE", "pieza": "B", "valor": "60"}).json()
    assert r["ok"] and r["respuesta"].startswith("DONE") and sim.angulos["B"] == 60, (r, sim.estado, sim.angulos)
    r = cliente.post("/manual/comando", data={"comando": "MOVE", "pieza": "M", "valor": "9999"}).json()
    assert not r["ok"] and r["respuesta"] == "NAK E05"
    r = cliente.post("/manual/comando", data={"comando": "SPIN", "valor": "60", "ms": "300", "quieto": "91"}).json()
    assert r["ok"] and sim.angulos["B"] == 91
    r = cliente.post("/manual/comando", data={"comando": "CFG", "pieza": "QUIETO_BRAZO", "valor": "92"}).json()
    assert r["ok"] and sim.cal["QUIETO_BRAZO"] == 92
    r = cliente.post("/manual/comando", data={"comando": "CFG", "pieza": "QUIETO_BRAZO"}).json()
    assert r["respuesta"] == "CFG QUIETO_BRAZO,92"
    assert cliente.post("/manual/comando", data={"comando": "POS", "valor": "0"}).json()["ok"]
    assert cliente.post("/manual/comando", data={"comando": "CFG", "pieza": "NADA"}).status_code == 400
    assert cliente.post("/manual/comando", data={"comando": "TURN"}).status_code == 400
    assert cliente.post("/manual/comando", data={"comando": "STATUS"}).json()["respuesta"].startswith("STATE")
