"""Protocolo P1 entre la Raspberry Pi y el ESP32 (Documento Técnico v2.2, sección 8).

Trama: ``$SEQ,CMD[,ARG1[,ARG2[,ARG3]]]*CS\\n``

* ``CS`` es el XOR, en hexadecimal de 2 dígitos, de todos los bytes entre ``$`` y ``*``.
* La trama mide 64 bytes como máximo (sin el ``\\n``).
* ``SEQ`` va de 1 a 255; el 0 se reserva para los eventos (``EVT``) del ESP32.
* El último argumento conserva las comas que contenga (igual que el firmware).
"""
from __future__ import annotations

from dataclasses import dataclass, field

TRAMA_MAX = 64
MAX_ARGS = 3

COMANDOS_MOVIMIENTO = frozenset({"HOME", "TURN", "RETRY", "BACK", "MOVE", "SPIN"})

# MOVE (firmware 2.3.0): control manual de una sola pieza desde el panel
PIEZAS_MOVE = {"B": "brazo", "R": "barra", "M": "motor del rodillo"}
MOVE_PASOS_MAX = 4096  # medios pasos: una vuelta del rodillo por orden
SPIN_MS_MAX = 10000  # SPIN: giro por tiempo del servo de 360° del brazo

# Tiempos máximos de la tabla 8.3 (segundos)
TIEMPO_MAXIMO = {
    "PING": 0.05,
    "STATUS": 0.05,
    "HOME": 3.0,
    "TURN": 10.0,
    "RETRY": 10.0,
    "BACK": 10.0,
    "MOVE": 28.0,  # tope; el ESP32 calcula el límite de cada orden según la velocidad calibrada
    "SPIN": 11.5,  # hasta 10 s de giro + margen
    "FREE": 0.05,
    "POS": 0.05,  # posición estimada del brazo de 360° (firmware 2.3.0)  # corta los pulsos de un servo (firmware 2.3.0)
    "LIGHT": 0.05,
    "LCD": 0.05,
    "BEEP": 0.05,
    "STOP": 0.1,
    "CFG": 0.05,
}

# Tabla 8.5
CODIGOS_ERROR = {
    "E01": "Suma de verificación incorrecta",
    "E02": "Comando desconocido",
    "E03": "Ocupado: llegó una orden de movimiento durante MOVIENDO",
    "E04": "La secuencia superó su tiempo máximo",
    "E05": "Argumento fuera de rango",
    "E06": "En PARADA: se requiere HOME",
    "E07": "El ESP32 se reinició de forma inesperada",
}


class ErrorTrama(ValueError):
    """La línea no es una trama válida. ``codigo`` es el NAK que correspondería."""

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo


@dataclass
class Trama:
    seq: int
    cmd: str
    args: list[str] = field(default_factory=list)

    @property
    def cuerpo(self) -> str:
        return ",".join([self.cmd, *self.args])

    def __str__(self) -> str:  # pragma: no cover - solo para registros
        return f"#{self.seq} {self.cuerpo}"


def checksum(texto: str) -> int:
    cs = 0
    for b in texto.encode("ascii"):
        cs ^= b
    return cs


def armar(seq: int, cmd: str, *args: object) -> str:
    """Devuelve la trama completa con ``\\n``. Lanza ``ValueError`` si supera 64 bytes."""
    if not 0 <= seq <= 255:
        raise ValueError(f"SEQ fuera de rango: {seq}")
    interior = ",".join([str(seq), cmd, *(str(a) for a in args)])
    for prohibido in "$*\n\r":
        if prohibido in interior:
            raise ValueError(f"carácter prohibido {prohibido!r} en la trama")
    trama = f"${interior}*{checksum(interior):02X}"
    if len(trama) > TRAMA_MAX:
        raise ValueError(f"la trama supera {TRAMA_MAX} bytes: {trama}")
    return trama + "\n"


def analizar(linea: str) -> Trama | None:
    """Analiza una línea (con o sin ``\\n``).

    Devuelve ``None`` si debe descartarse en silencio (no empieza con ``$`` o
    supera 64 bytes) y lanza ``ErrorTrama`` si la suma o el formato fallan.
    """
    linea = linea.rstrip("\r\n")
    if not linea or len(linea) > TRAMA_MAX or not linea.startswith("$"):
        return None
    asterisco = linea.rfind("*")
    if asterisco < 0 or len(linea) - asterisco != 3:
        raise ErrorTrama("E02", f"trama sin suma de verificación: {linea!r}")
    interior = linea[1:asterisco]
    try:
        esperado = int(linea[asterisco + 1 :], 16)
    except ValueError as exc:
        raise ErrorTrama("E02", f"suma no hexadecimal: {linea!r}") from exc
    try:
        calculado = checksum(interior)
    except UnicodeEncodeError as exc:
        raise ErrorTrama("E02", f"caracteres no ASCII: {linea!r}") from exc
    if calculado != esperado:
        raise ErrorTrama("E01", f"suma incorrecta en {linea!r}: esperado {calculado:02X}")

    partes = interior.split(",", 1 + MAX_ARGS)
    if len(partes) < 2 or not partes[0].isdigit() or len(partes[0]) > 3:
        raise ErrorTrama("E02", f"formato inválido: {linea!r}")
    seq = int(partes[0])
    if seq > 255 or len(partes[1]) < 2:
        raise ErrorTrama("E02", f"formato inválido: {linea!r}")
    return Trama(seq=seq, cmd=partes[1], args=partes[2:])


def texto_lcd(texto: str) -> str:
    """Adapta un texto a la LCD 16x2: mayúsculas ASCII sin tildes, máx. 16 caracteres."""
    reemplazos = str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNaeiouun")
    limpio = texto.translate(reemplazos).upper()
    limpio = "".join(c for c in limpio if 32 <= ord(c) < 127 and c not in "$*")
    return limpio[:16]
