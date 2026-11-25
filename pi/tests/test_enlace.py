"""Pruebas de comunicación TC.1 a TC.6 (tabla 8.9) contra el ESP32 simulado."""
import time

import pytest

from archilector.enlace import protocolo as P
from archilector.enlace.esp32 import EnlaceESP32, RechazoESP32
from archilector.enlace.simulador import SimuladorESP32
from archilector.simulacion import LibroSimulado


@pytest.fixture
def sim():
    s = SimuladorESP32(libro=LibroSimulado(), escala_tiempo=0.05)
    yield s
    s.close()


@pytest.fixture
def enlace(sim):
    e = EnlaceESP32(sim, latido=False)
    yield e
    e.cerrar()


def _leer_respuestas(sim, segundos=0.5):
    fin, salida = time.monotonic() + segundos, []
    while time.monotonic() < fin:
        linea = sim.readline()
        if linea:
            t = P.analizar(linea.decode())
            if t.seq:
                salida.append(t.cuerpo)
    return salida


def test_T2_1_ping(enlace):
    r = enlace.comando("PING")
    assert r.cmd == "PONG" and r.args[1] == "P1"
    assert r.latencia < 0.05


def test_TC1_checksum_incorrecto(sim):
    sim.write(b"$17,TURN*00\n")
    assert _leer_respuestas(sim, 0.2) == ["NAK,E01"]
    assert sim.ordenes_ejecutadas == []


def test_TC2_duplicado_un_solo_volteo(sim):
    sim.write(b"$17,TURN*37\n")
    sim.write(b"$17,TURN*37\n")
    respuestas = _leer_respuestas(sim, 1.0)
    assert sim.ordenes_ejecutadas == ["TURN"]
    assert respuestas[0] == "ACK" and respuestas[1] == "ACK"
    assert respuestas[-1].startswith("DONE")
    # Tras terminar, el mismo SEQ devuelve el DONE guardado sin moverse
    sim.write(b"$17,TURN*37\n")
    assert _leer_respuestas(sim, 0.2)[0].startswith("DONE")
    assert sim.ordenes_ejecutadas == ["TURN"]


def test_TC3_ocupado(enlace, sim):
    import threading

    hilo = threading.Thread(target=enlace.comando, args=("TURN",))
    hilo.start()
    time.sleep(0.05)
    with pytest.raises(RechazoESP32) as e:
        enlace._comando("TURN", (), None)  # sin el candado de una-orden-a-la-vez
    assert e.value.codigo == "E03"
    hilo.join()


def test_TC4_corte_de_enlace():
    sim = SimuladorESP32(escala_tiempo=0.05, t_vigilancia=0.3)
    enlace = EnlaceESP32(sim, latido=False)
    enlace.comando("PING")
    time.sleep(0.6)
    assert sim.estado == "PARADA"
    assert sim.lcd[0] == "SIN ENLACE"
    ev = [enlace.eventos.get(timeout=0.5) for _ in range(2)]
    assert any(e.tipo == "STATE" and e.valor == "PARADA" for e in ev)
    enlace.cerrar()


def test_TC5_parada_durante_el_giro(enlace, sim):
    import threading

    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.setdefault("r", enlace.comando("TURN")))
    hilo.start()
    time.sleep(0.1)
    r = enlace.comando("STOP")
    assert r.cmd == "STATE" and r.args == ["PARADA"]
    assert r.latencia < 0.1
    hilo.join()
    assert resultado["r"].cmd == "FAIL" and resultado["r"].codigo == "E06"
    # En PARADA solo HOME saca al ESP32
    with pytest.raises(RechazoESP32) as e:
        enlace.comando("TURN")
    assert e.value.codigo == "E06"
    assert enlace.comando("HOME").ok
    assert enlace.comando("TURN").ok


def test_TC6_mil_ping(enlace):
    for _ in range(1000):
        assert enlace.comando("PING").cmd == "PONG"


def test_tecla_D_y_eventos(enlace, sim):
    sim.pulsar_tecla("D")
    eventos = [enlace.eventos.get(timeout=0.5) for _ in range(3)]
    valores = [(e.tipo, e.valor) for e in eventos]
    assert ("STATE", "PARADA") in valores and ("KEY", "D") in valores


def test_E07_reinicio(enlace, sim):
    while not enlace.eventos.empty():
        enlace.eventos.get_nowait()
    sim.reiniciar_inesperado()
    ev = enlace.eventos.get(timeout=0.5)
    assert ev.valor == "ARRANQUE" and ev.extra == ["E07"]


def test_retry_y_cfg(enlace, sim):
    assert enlace.comando("CFG", "PASOS_ARCO", 1100).cmd == "ACK"
    assert sim.cal["PASOS_ARCO"] == 1100
    with pytest.raises(RechazoESP32):
        enlace.comando("CFG", "PASOS_ARCO", 99999)
    assert enlace.comando("RETRY", 40).ok
    assert sim.ordenes_ejecutadas[-1] == "RETRY,40"
