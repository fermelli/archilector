"""Enlace con el ESP32 por USB serie (interfaz I1, sección 8 del Documento Técnico).

Reglas implementadas (sección 8.6):

* Espera de ACK de 200 ms; si no llega, reenvía la misma trama con el mismo SEQ
  hasta 3 veces y después declara el enlace caído.
* ``NAK,E01`` → reenvía la misma trama.
* Latido: ``PING`` cada segundo desde un hilo propio si no se envió otra trama.
  Se mantiene también durante los movimientos, porque el ESP32 pasa a PARADA
  tras 5 s sin tramas válidas y un volteo dura unos 6 s.
* Los eventos ``EVT`` (SEQ 0) se entregan por una cola y un callback.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import protocolo as P

log = logging.getLogger(__name__)

ESPERA_ACK = 0.2
REENVIOS = 3
MARGEN_MOVIMIENTO = 2.0  # segundos sobre el tiempo máximo de la tabla 8.3
PERIODO_LATIDO = 1.0


class ErrorEnlace(Exception):
    pass


class EnlaceCaido(ErrorEnlace):
    pass


class RechazoESP32(ErrorEnlace):
    """El ESP32 respondió NAK."""

    def __init__(self, cmd: str, codigo: str):
        super().__init__(f"{cmd} rechazado: {codigo} ({P.CODIGOS_ERROR.get(codigo, '?')})")
        self.cmd = cmd
        self.codigo = codigo


@dataclass
class Respuesta:
    cmd: str
    args: list[str]
    latencia: float  # segundos desde el envío hasta la respuesta final

    @property
    def ok(self) -> bool:
        return self.cmd != "FAIL"

    @property
    def codigo(self) -> str | None:
        return self.args[0] if self.cmd == "FAIL" and self.args else None

    @property
    def duracion_ms(self) -> int | None:
        if self.cmd == "DONE" and self.args:
            try:
                return int(self.args[0])
            except ValueError:
                return None
        return None


@dataclass
class Evento:
    tipo: str          # "KEY" o "STATE"
    valor: str         # tecla o estado
    extra: list[str]   # p. ej. ["E07"]


def abrir_puerto(puerto: str, **opciones_sim):
    """Abre el puerto real o, con ``sim://``, un ESP32 simulado."""
    if puerto.startswith("sim://"):
        from .simulador import SimuladorESP32

        return SimuladorESP32(**opciones_sim)
    import serial  # pyserial

    return serial.Serial(puerto, 115200, bytesize=8, parity="N", stopbits=1, timeout=0.05)


class EnlaceESP32:
    def __init__(self, puerto, latido: bool = True, al_evento: Callable[[Evento], None] | None = None):
        """``puerto`` es un objeto tipo serial (``write``/``readline``) ya abierto."""
        self.puerto = puerto
        self.al_evento = al_evento
        self.eventos: queue.Queue[Evento] = queue.Queue()
        self.version_firmware: str | None = None
        self.version_protocolo: str | None = None
        self.caido = False
        self._seq = random.randint(1, 255)  # evita confundir órdenes viejas si la Pi reinicia
        self._pendientes: dict[int, queue.Queue[P.Trama]] = {}
        self._lock_envio = threading.Lock()
        self._lock_seq = threading.Lock()
        self._lock_orden = threading.Lock()  # una orden de movimiento a la vez
        self._ultimo_envio = 0.0
        self._activo = threading.Event()
        self._activo.set()
        self._lector = threading.Thread(target=self._leer, daemon=True, name="esp32-lector")
        self._lector.start()
        self._latido = None
        if latido:
            self._latido = threading.Thread(target=self._latir, daemon=True, name="esp32-latido")
            self._latido.start()

    # --- ciclo de vida -------------------------------------------------------
    def cerrar(self) -> None:
        self._activo.clear()
        try:
            self.puerto.close()
        except Exception:  # pragma: no cover
            pass

    def conectar(self) -> str:
        """Envía PING y registra las versiones (sección 10.3)."""
        r = self.comando("PING")
        if len(r.args) >= 2:
            self.version_firmware, self.version_protocolo = r.args[0], r.args[1]
        log.info("ESP32 conectado: firmware %s, protocolo %s", self.version_firmware, self.version_protocolo)
        return f"{self.version_firmware},{self.version_protocolo}"

    # --- envío ---------------------------------------------------------------
    def _siguiente_seq(self) -> int:
        with self._lock_seq:
            for _ in range(255):
                self._seq = self._seq % 255 + 1  # 1..255, nunca 0
                if self._seq not in self._pendientes:
                    return self._seq
        raise ErrorEnlace("no hay SEQ libres")

    def _escribir(self, trama: str) -> None:
        with self._lock_envio:
            self.puerto.write(trama.encode("ascii"))
            self._ultimo_envio = time.monotonic()

    def escribir_crudo(self, texto: str) -> None:
        """Escribe bytes tal cual."""
        self._escribir(texto)

    def trama_cruda(self, seq: int, texto: str, segundos: float, repeticiones: int = 1) -> list[str]:
        """Escribe una trama armada a mano y junta durante ``segundos`` todas las
        respuestas con ese SEQ (pruebas TC.1 y TC.2)."""
        cola: queue.Queue[P.Trama] = queue.Queue()
        self._pendientes[seq] = cola
        try:
            for _ in range(repeticiones):
                self._escribir(texto)
            fin, respuestas = time.monotonic() + segundos, []
            while (resto := fin - time.monotonic()) > 0:
                try:
                    respuestas.append(cola.get(timeout=resto).cuerpo)
                except queue.Empty:
                    break
            return respuestas
        finally:
            self._pendientes.pop(seq, None)

    def comando(self, cmd: str, *args: object, timeout: float | None = None) -> Respuesta:
        """Envía una orden y devuelve su respuesta final.

        Para HOME/TURN/RETRY/BACK espera ACK y luego DONE o FAIL. Lanza
        ``RechazoESP32`` ante un NAK y ``EnlaceCaido`` si no hay respuesta.
        """
        if cmd in P.COMANDOS_MOVIMIENTO:
            with self._lock_orden:
                return self._comando(cmd, args, timeout)
        return self._comando(cmd, args, timeout)

    def _comando(self, cmd: str, args: tuple, timeout: float | None) -> Respuesta:
        seq = self._siguiente_seq()
        trama = P.armar(seq, cmd, *args)
        cola: queue.Queue[P.Trama] = queue.Queue()
        self._pendientes[seq] = cola
        t0 = time.monotonic()
        try:
            primera = None
            intentos = 0
            while primera is None:
                if intentos > REENVIOS:
                    self._marcar_caido(f"sin respuesta a {cmd}")
                    raise EnlaceCaido(f"el ESP32 no respondió a {cmd} tras {REENVIOS} reenvíos")
                self._escribir(trama)
                intentos += 1
                try:
                    r = cola.get(timeout=ESPERA_ACK)
                except queue.Empty:
                    continue
                if r.cmd == "NAK" and r.args and r.args[0] == "E01":
                    log.warning("NAK,E01 a %s: se reenvía", cmd)
                    continue
                primera = r
            self.caido = False

            if primera.cmd == "NAK":
                raise RechazoESP32(cmd, primera.args[0] if primera.args else "?")
            if cmd not in P.COMANDOS_MOVIMIENTO:
                return Respuesta(primera.cmd, primera.args, time.monotonic() - t0)

            # Movimiento: después del ACK llega DONE o FAIL
            limite = timeout or (P.TIEMPO_MAXIMO[cmd] + MARGEN_MOVIMIENTO)
            final = primera
            while final.cmd == "ACK":
                try:
                    final = cola.get(timeout=limite)
                except queue.Empty:
                    raise ErrorEnlace(f"{cmd}: no llegó DONE ni FAIL en {limite:.1f} s") from None
            return Respuesta(final.cmd, final.args, time.monotonic() - t0)
        finally:
            self._pendientes.pop(seq, None)

    def _marcar_caido(self, motivo: str) -> None:
        if not self.caido:
            log.error("Enlace con el ESP32 caído: %s", motivo)
        self.caido = True

    # --- hilos ---------------------------------------------------------------
    def _leer(self) -> None:
        while self._activo.is_set():
            try:
                crudo = self.puerto.readline()
            except Exception as exc:  # puerto desconectado
                self._marcar_caido(str(exc))
                time.sleep(0.5)
                continue
            if not crudo:
                continue
            linea = crudo.decode("ascii", "replace")
            try:
                t = P.analizar(linea)
            except P.ErrorTrama as exc:
                log.warning("Trama inválida del ESP32: %s", exc)
                continue
            if t is None:
                continue  # mensajes de arranque del bootloader, etc.
            if t.seq == 0:
                self._despachar_evento(t)
            else:
                cola = self._pendientes.get(t.seq)
                if cola is not None:
                    cola.put(t)
                else:
                    log.debug("Respuesta sin orden pendiente: %s", t)

    def _despachar_evento(self, t: P.Trama) -> None:
        if t.cmd != "EVT" or not t.args:
            return
        tipo, valor, *extra = t.args[0], (t.args[1] if len(t.args) > 1 else ""), *t.args[2:]
        # El firmware manda "EVT,STATE,ARRANQUE,E07": el último argumento conserva comas
        if extra and "," in extra[-1]:
            extra = extra[:-1] + extra[-1].split(",")
        ev = Evento(tipo, valor, list(extra))
        log.info("Evento del ESP32: %s %s %s", ev.tipo, ev.valor, ev.extra or "")
        self.eventos.put(ev)
        if self.al_evento:
            try:
                self.al_evento(ev)
            except Exception:  # pragma: no cover
                log.exception("Error en el manejador de eventos")

    def _latir(self) -> None:
        while self._activo.is_set():
            time.sleep(0.1)
            if time.monotonic() - self._ultimo_envio < PERIODO_LATIDO:
                continue
            try:
                self._comando("PING", (), None)
            except ErrorEnlace:
                time.sleep(PERIODO_LATIDO)
