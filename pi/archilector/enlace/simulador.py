"""ESP32 simulado con la misma lógica que el firmware (``firmware/esp32``).

Se comporta como un puerto serie (``write``/``readline``) para que
``EnlaceESP32`` no note la diferencia. Implementa la trama P1, la caché de SEQ,
la máquina de estados de la figura 8.3, los NAK E01–E06, la vigilancia de 5 s,
la parada inmediata y los tiempos de la secuencia de volteo.

Se usa con el puerto ``sim://``.
"""
from __future__ import annotations

import queue
import threading
import time

from . import protocolo as P

VERSION_FIRMWARE = "2.2.0-sim"

CALIBRACION_INICIAL = {
    "ANG_REPOSO": 90,
    "ANG_CONTACTO": 0,
    "ANG_DEPOSITO": 180,
    "PASOS_ARCO": 1040,
    "VEL_GIRO": 120,
    "BARRA_ABIERTA": 70,
    "BARRA_CERRADA": 0,
    "VEL_BARRA": 180,
    "VEL_RODILLO": 600,
    "DIR_RODILLO": 1,
    "EMBRAGUE": 0,
    "T_ASENTAR": 250,
    "PULSO_MIN": 500,
    "PULSO_MAX": 2500,
}
RANGOS = {
    "ANG_REPOSO": (0, 180), "ANG_CONTACTO": (0, 180), "ANG_DEPOSITO": (0, 180),
    "PASOS_ARCO": (0, 8192), "VEL_GIRO": (10, 360), "BARRA_ABIERTA": (0, 180),
    "BARRA_CERRADA": (0, 180), "VEL_BARRA": (10, 360), "VEL_RODILLO": (50, 1000),
    "DIR_RODILLO": (-1, 1), "EMBRAGUE": (0, 1), "T_ASENTAR": (0, 2000),
    "PULSO_MIN": (400, 1200), "PULSO_MAX": (1800, 2600),
}


class _Parada(Exception):
    pass


class _TiempoExcedido(Exception):
    pass


class SimuladorESP32:
    """Puerto serie falso conectado a un ESP32 simulado."""

    def __init__(self, libro=None, escala_tiempo: float = 1.0, t_vigilancia: float = 5.0):
        self.libro = libro
        self.escala = escala_tiempo
        self.t_vigilancia = t_vigilancia
        self.cal = dict(CALIBRACION_INICIAL)
        self.estado = "ARRANQUE"
        self.error = 0
        self.luz = 0
        self.lcd = ["ARCHILECTOR", "ARRANQUE"]
        self.pitidos: list[int] = []
        self.ordenes_ejecutadas: list[str] = []  # registro para las pruebas
        self.timeout = 0.05
        self._salida: queue.Queue[bytes] = queue.Queue()
        self._entrada = bytearray()
        self._cache: dict[int, tuple[float, str]] = {}
        self._abortar = threading.Event()
        self._lock = threading.RLock()
        self._ultima_valida = 0.0
        self._vigilancia_armada = False
        self._cerrado = threading.Event()
        threading.Thread(target=self._vigilar, daemon=True, name="sim-vigilancia").start()
        # Arranque: servos a reposo y aviso a la Pi
        self.estado = "REPOSO"
        self.lcd = ["LISTO", "A=INICIAR"]
        self._enviar(0, "EVT,STATE,REPOSO")

    # --- interfaz de puerto serie ------------------------------------------
    @property
    def is_open(self) -> bool:
        return not self._cerrado.is_set()

    def write(self, datos: bytes) -> int:
        self._entrada.extend(datos)
        while b"\n" in self._entrada:
            linea, _, resto = bytes(self._entrada).partition(b"\n")
            self._entrada = bytearray(resto)
            self._procesar_linea(linea.decode("ascii", "replace").rstrip("\r"))
        return len(datos)

    def readline(self) -> bytes:
        try:
            return self._salida.get(timeout=self.timeout)
        except queue.Empty:
            return b""

    def reset_input_buffer(self) -> None:
        while not self._salida.empty():
            self._salida.get_nowait()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self._cerrado.set()
        self._abortar.set()

    # --- acciones "físicas" para pruebas ----------------------------------
    def pulsar_tecla(self, tecla: str) -> None:
        if tecla == "D":
            self._entrar_parada("PARADA", notificar=True)
        self._enviar(0, f"EVT,KEY,{tecla}")

    def reiniciar_inesperado(self) -> None:
        """Simula una caída de tensión (E07)."""
        self._abortar.set()
        with self._lock:
            self._cache.clear()
            self.error = 7
            self.estado = "REPOSO"
            self._vigilancia_armada = False
        self._enviar(0, "EVT,STATE,ARRANQUE,E07")
        self._enviar(0, "EVT,STATE,REPOSO")

    # --- protocolo ---------------------------------------------------------
    def _enviar(self, seq: int, cuerpo: str) -> None:
        if seq and not cuerpo.startswith("NAK"):
            with self._lock:
                previo = self._cache.get(seq)
                marca = previo[0] if previo else time.monotonic()
                self._cache[seq] = (marca, cuerpo)
                if len(self._cache) > 8:
                    del self._cache[min(self._cache, key=lambda k: self._cache[k][0])]
        cmd, *args = cuerpo.split(",")
        self._salida.put(P.armar(seq, cmd, *args).encode("ascii"))

    def _procesar_linea(self, linea: str) -> None:
        try:
            t = P.analizar(linea)
        except P.ErrorTrama as e:
            seq = int(linea[1:4].split(",")[0]) if linea[1:2].isdigit() else 0
            self._enviar(seq if seq <= 255 else 0, f"NAK,{e.codigo}")
            return
        if t is None:
            return
        with self._lock:
            self._ultima_valida = time.monotonic()
            self._vigilancia_armada = True
            guardada = self._cache.get(t.seq) if t.seq else None
        if guardada and time.monotonic() - guardada[0] < 15.0:
            self._enviar(t.seq, guardada[1])
            return
        self._procesar(t)

    def _procesar(self, t: P.Trama) -> None:
        c = t.cmd
        if c == "PING":
            return self._enviar(t.seq, f"PONG,{VERSION_FIRMWARE},P1")
        if c == "STATUS":
            return self._enviar(t.seq, f"STATE,{self.estado},E{self.error:02d}")
        if c == "STOP":
            self._entrar_parada("PARADA", notificar=False)
            return self._enviar(t.seq, "STATE,PARADA")
        if c in P.COMANDOS_MOVIMIENTO:
            return self._orden_movimiento(t)
        if c == "LIGHT":
            v = _entero(t.args, 0)
            if len(t.args) != 1 or v is None or not 0 <= v <= 100:
                return self._enviar(t.seq, "NAK,E05")
            self.luz = v
            return self._enviar(t.seq, "ACK")
        if c == "LCD":
            linea = _entero(t.args, 0)
            texto = ",".join(t.args[1:])
            if len(t.args) < 2 or linea not in (1, 2) or len(texto) > 16:
                return self._enviar(t.seq, "NAK,E05")
            self.lcd[linea - 1] = texto
            return self._enviar(t.seq, "ACK")
        if c == "BEEP":
            p = _entero(t.args, 0)
            if len(t.args) != 1 or p not in (1, 2, 3):
                return self._enviar(t.seq, "NAK,E05")
            self.pitidos.append(p)
            return self._enviar(t.seq, "ACK")
        if c == "CFG":
            if self.estado == "MOVIENDO":
                return self._enviar(t.seq, "NAK,E03")
            v = _entero(t.args, 1)
            if len(t.args) != 2 or t.args[0] not in RANGOS or v is None:
                return self._enviar(t.seq, "NAK,E05")
            lo, hi = RANGOS[t.args[0]]
            if not lo <= v <= hi or (t.args[0] == "DIR_RODILLO" and v == 0):
                return self._enviar(t.seq, "NAK,E05")
            self.cal[t.args[0]] = v
            return self._enviar(t.seq, "ACK")
        self._enviar(t.seq, "NAK,E02")

    def _orden_movimiento(self, t: P.Trama) -> None:
        with self._lock:
            if self.estado in ("MOVIENDO", "ARRANQUE"):
                return self._enviar(t.seq, "NAK,E03")
            extra = 0
            if t.cmd != "HOME":
                if self.estado in ("PARADA", "FALLA"):
                    return self._enviar(t.seq, "NAK,E06")
                if t.cmd == "RETRY":
                    extra = _entero(t.args, 0)
                    if len(t.args) != 1 or extra is None or not 0 <= extra <= 2000:
                        return self._enviar(t.seq, "NAK,E05")
                if t.cmd == "BACK" and self.cal["EMBRAGUE"]:
                    return self._enviar(t.seq, "NAK,E05")
            self._abortar.clear()
            self.estado = "MOVIENDO"
        self._enviar(t.seq, "ACK")
        self.ordenes_ejecutadas.append(t.cmd if t.cmd != "RETRY" else f"RETRY,{extra}")
        threading.Thread(target=self._mover, args=(t.cmd, t.seq, extra), daemon=True).start()

    # --- movimiento --------------------------------------------------------
    def _esperar(self, segundos: float, inicio: float, limite: float) -> None:
        fin = time.monotonic() + segundos * self.escala
        while True:
            if self._abortar.is_set():
                raise _Parada
            if time.monotonic() - inicio > limite * self.escala:
                raise _TiempoExcedido
            resto = fin - time.monotonic()
            if resto <= 0:
                return
            time.sleep(min(resto, 0.005))

    def _mover(self, cmd: str, seq: int, extra: int) -> None:
        c = self.cal
        giro = lambda a, b: abs(a - b) / c["VEL_GIRO"]  # noqa: E731
        barra = abs(c["BARRA_ABIERTA"] - c["BARRA_CERRADA"]) / c["VEL_BARRA"]
        asentar = c["T_ASENTAR"] / 1000
        rodillo = (c["PASOS_ARCO"] + extra) / c["VEL_RODILLO"]
        inicio = time.monotonic()
        limite = 3.0 if cmd == "HOME" else 10.0
        try:
            if cmd == "HOME":
                self._esperar(barra * 0.5 + giro(90, c["ANG_REPOSO"]) + 0.3, inicio, limite)
            elif cmd in ("TURN", "RETRY"):
                self._esperar(giro(c["ANG_REPOSO"], c["ANG_CONTACTO"]) + asentar + rodillo + barra, inicio, limite)
                if self.libro is not None:
                    self.libro.voltear()
                self._esperar(giro(c["ANG_CONTACTO"], c["ANG_DEPOSITO"]) + asentar + barra
                              + giro(c["ANG_DEPOSITO"], c["ANG_REPOSO"]), inicio, limite)
            elif cmd == "BACK":
                self._esperar(barra + giro(c["ANG_REPOSO"], c["ANG_DEPOSITO"]) + asentar
                              + c["PASOS_ARCO"] / c["VEL_RODILLO"], inicio, limite)
                if self.libro is not None:
                    self.libro.regresar()
                self._esperar(giro(c["ANG_DEPOSITO"], c["ANG_CONTACTO"]) + asentar + barra
                              + giro(c["ANG_CONTACTO"], c["ANG_REPOSO"]), inicio, limite)
        except _Parada:
            self._enviar(seq, "FAIL,E06")
            return
        except _TiempoExcedido:
            with self._lock:
                self.estado, self.error = "FALLA", 4
                self.lcd = ["ERROR E04", "D=PARAR"]
            self.pitidos.append(3)
            self._enviar(seq, "FAIL,E04")
            return
        with self._lock:
            if self._abortar.is_set():
                self._enviar(seq, "FAIL,E06")
                return
            self.estado, self.error = "REPOSO", 0
            self.lcd = ["LISTO", "A=INICIAR"]
        duracion_ms = int((time.monotonic() - inicio) * 1000 / max(self.escala, 1e-6))
        self._enviar(seq, f"DONE,{duracion_ms}")

    # --- parada y vigilancia ------------------------------------------------
    def _entrar_parada(self, linea1: str, notificar: bool) -> None:
        self._abortar.set()
        with self._lock:
            self.estado = "PARADA"
            self.lcd = [linea1, "A=REANUDAR"]
        self.pitidos.append(2)
        if notificar:
            self._enviar(0, "EVT,STATE,PARADA")

    def _vigilar(self) -> None:
        while not self._cerrado.wait(0.05):
            with self._lock:
                vencida = (self._vigilancia_armada and self.estado != "PARADA"
                           and time.monotonic() - self._ultima_valida > self.t_vigilancia)
            if vencida:
                self._entrar_parada("SIN ENLACE", notificar=True)


def _entero(args: list[str], i: int) -> int | None:
    try:
        return int(args[i])
    except (IndexError, ValueError):
        return None
