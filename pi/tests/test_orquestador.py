"""Ciclo completo contra el ESP32 simulado: manejo de cada clase del
verificador según la figura 8.2 y la tabla 6 del Diseño."""
import time
from collections import deque

import pytest

from archilector import config as configuracion
from archilector.camara import CamaraSimulada
from archilector.enlace.esp32 import EnlaceESP32
from archilector.enlace.simulador import SimuladorESP32
from archilector.ia.clasificador import Clasificador
from archilector.ia.registro_pares import RegistroPares
from archilector.ia.verificador import Verificador, Veredicto
from archilector.orquestador import ARCHIVO, ASISTIDO, LECTURA, Orquestador
from archilector.salida.db import BaseDatos
from archilector.simulacion import LibroSimulado
from archilector.vision.ocr import OCRNulo
from archilector.voz.tts import VozConsola


class VerificadorGuionado(Verificador):
    """Responde las clases de un guion; después, página nueva."""

    def __init__(self, *clases):
        super().__init__(None)
        self.guion = deque(clases)

    def verificar(self, antes, despues):
        v = super().verificar(antes, despues)
        clase = self.guion.popleft() if self.guion else "pagina_nueva"
        return Veredicto(clase, 0.95, "guion", regla=v.regla, imagen_compuesta=v.imagen_compuesta)


def armar(tmp_path, verificador, escala=0.01):
    cfg = configuracion.cargar(None)
    cfg["datos"] = str(tmp_path)
    cfg["camara"]["espera_luz"] = 0
    libro = LibroSimulado(hojas=30)
    sim = SimuladorESP32(libro=libro, escala_tiempo=escala)
    db = BaseDatos(tmp_path / "archilector.db")
    o = Orquestador(cfg, None, CamaraSimulada(libro), OCRNulo(), verificador, Clasificador(None), db,
                    VozConsola(), RegistroPares(tmp_path / "pares"))
    enlace = EnlaceESP32(sim, latido=True, al_evento=o.al_evento)
    o.enlace = enlace
    return o, sim, libro, db


def esperar_estado(o, estado, timeout=10.0):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if o.publico.estado == estado:
            return
        time.sleep(0.01)
    raise AssertionError(f"no llegó a {estado!r}; está en {o.publico.estado!r}")


def test_archivo_sin_fallas_genera_pdf(tmp_path):
    o, sim, libro, db = armar(tmp_path, VerificadorGuionado())
    doc = o.iniciar_documento("Actas 1998", "actas", ARCHIVO, max_paginas=3)
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == ["TURN", "TURN"]
    assert libro.hoja == 2
    d = db.documento(doc)
    assert d["paginas"] == 3 and d["ruta_pdf"].endswith(".pdf")
    assert (tmp_path / "documentos" / str(doc) / f"documento_{doc}.pdf").stat().st_size > 1000
    assert len(db.volteos(doc)) == 2
    assert db.conteo_pares()["sin_etiqueta"] == 2


def test_misma_pagina_reintenta_con_retry_40(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado("misma_pagina", "misma_pagina"))
    o.iniciar_documento("x", "l", ARCHIVO, max_paginas=2)
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == ["TURN", "RETRY,40", "RETRY,40"]


def test_tras_dos_reintentos_se_detiene_y_pide_ayuda(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado("misma_pagina", "misma_pagina", "misma_pagina"))
    o.iniciar_documento("x", "l", LECTURA, max_paginas=2)
    esperar_estado(o, "requiere_usuario")
    assert sim.lcd[0] == "NO PASA LA HOJA" and sim.pitidos[-1] == 3
    assert any("Pásela a mano" in d for d in o.voz.dicho)  # en lectura avisa por voz
    o.orden("confirmar")
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == ["TURN", "RETRY,40", "RETRY,40"]
    assert o.publico.paginas == 2


def test_dos_hojas_envia_back(tmp_path):
    o, sim, libro, _ = armar(tmp_path, VerificadorGuionado("dos_hojas"))
    libro.forzar("dos_hojas")
    o.iniciar_documento("x", "l", ARCHIVO, max_paginas=2)
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == ["TURN", "BACK"]
    assert libro.hoja == 1  # pasaron dos y BACK devolvió una


def test_hoja_doblada_espera_numeral(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado("hoja_doblada"))
    o.iniciar_documento("x", "l", ARCHIVO, max_paginas=2)
    esperar_estado(o, "requiere_usuario")
    assert sim.lcd == ["REVISE LA HOJA", "#=CONTINUAR"]
    o.orden("confirmar")
    o.esperar_fin(20)
    assert o.publico.paginas == 2


def test_tecla_D_detiene_y_A_reanuda_con_home(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado(), escala=0.2)
    o.iniciar_documento("x", "l", ARCHIVO, max_paginas=2)
    esperar_estado(o, "volteando")
    time.sleep(0.2)
    sim.pulsar_tecla("D")
    esperar_estado(o, "parada")
    assert sim.estado == "PARADA"
    o.orden("iniciar")
    o.esperar_fin(30)
    assert "HOME" in sim.ordenes_ejecutadas
    assert o.publico.paginas == 2


def test_numeral_en_pausa_termina_documento(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado(), escala=0.2)
    doc = o.iniciar_documento("x", "l", ARCHIVO)
    esperar_estado(o, "volteando")
    o.orden("pausa")
    esperar_estado(o, "pausa", timeout=15)
    o.orden("confirmar")
    o.esperar_fin(20)
    assert o.publico.estado == "terminado"
    assert o.db.documento(doc)["terminado"]


def test_lectura_lee_cada_pagina(tmp_path):
    o, sim, *_ = armar(tmp_path, VerificadorGuionado())
    o.iniciar_documento("x", "l", LECTURA, max_paginas=2)
    o.esperar_fin(20)
    # las dos páginas se leen, incluida la última (antes el límite cortaba antes de leerla)
    assert sum("No se reconoció texto" in d for d in o.voz.dicho) == 2


def test_asistido_detecta_el_cambio(tmp_path):
    o, sim, libro, _ = armar(tmp_path, VerificadorGuionado())
    o.iniciar_documento("x", "l", ASISTIDO, max_paginas=2)
    esperar_estado(o, "esperando_hoja")
    libro.voltear()  # la persona pasa la hoja
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == []  # el ESP32 no se movió
    assert o.publico.paginas == 2


class OCRDelLibro:
    """OCR falso que 'lee' el número de la página derecha del libro simulado,
    salvo en las páginas indicadas (para imitar números ilegibles)."""

    def __init__(self, libro, sin_numero=()):
        self.libro, self.sin_numero = libro, set(sin_numero)

    def leer(self, pagina):
        from archilector.vision.ocr import ResultadoOCR

        n = self.libro.pagina_derecha
        return ResultadoOCR(numero_pagina=None if n in self.sin_numero else n)


def test_dos_hojas_por_numeracion_aunque_falte_un_numero(tmp_path):
    # Sin modelo: la regla de píxeles da por buenas las dos hojas; la numeración las delata
    o, sim, libro, _ = armar(tmp_path, Verificador(None))
    o.ocr = OCRDelLibro(libro, sin_numero={3})
    libro.forzar("ok", "dos_hojas")
    o.iniciar_documento("x", "l", ARCHIVO, max_paginas=3)
    o.esperar_fin(20)
    assert sim.ordenes_ejecutadas == ["TURN", "TURN", "BACK"]
    assert libro.pagina_derecha == 5
