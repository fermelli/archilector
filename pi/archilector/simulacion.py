"""Libro simulado para desarrollar y probar sin hardware.

El simulador del ESP32 lo "voltea" y la cámara simulada lo "fotografía". Así
todo el ciclo (volteo → foto → verificador → OCR → PDF) se puede ejecutar en
un PC. Se pueden forzar resultados de volteo para probar el manejo de fallas
de la tabla 6 del Documento de Diseño.
"""
from __future__ import annotations

import random
import threading
from collections import deque

import cv2
import numpy as np

# Resultados posibles de un volteo físico
VOLTEO_OK = "ok"
VOLTEO_NO_PASA = "no_pasa"
VOLTEO_DOS_HOJAS = "dos_hojas"
VOLTEO_DOBLADA = "doblada"

_FRASES = [
    "El papel encuadernado solo se puede leer pasando sus paginas a mano.",
    "ArchiLector fotografia cada pagina y la convierte en un PDF buscable.",
    "La Raspberry Pi ve, lee, verifica y habla; el ESP32 pasa la pagina.",
    "Los rodillos de recogida arquean la hoja y el brazo la lleva al otro lado.",
    "El verificador compara la foto de antes con la de despues de cada volteo.",
    "Si el volteo fallo, la Pi envia RETRY con parametros ajustados.",
]


class LibroSimulado:
    """Libro de ``hojas`` hojas. La página derecha visible tiene número impar."""

    def __init__(self, hojas: int = 20, semilla: int = 7, prob_fallas: dict[str, float] | None = None):
        self.hojas = hojas
        self.hoja = 0  # hojas ya pasadas a la izquierda
        self.doblada = False
        self.forzados: deque[str] = deque()
        self.prob_fallas = prob_fallas or {}
        self._azar = random.Random(semilla)
        self._lock = threading.Lock()

    # --- mecánica ---------------------------------------------------------
    def forzar(self, *resultados: str) -> None:
        """Encola resultados para los próximos volteos (para pruebas)."""
        self.forzados.extend(resultados)

    def _elegir_resultado(self) -> str:
        if self.forzados:
            return self.forzados.popleft()
        r = self._azar.random()
        acumulado = 0.0
        for resultado, p in self.prob_fallas.items():
            acumulado += p
            if r < acumulado:
                return resultado
        return VOLTEO_OK

    def voltear(self) -> str:
        with self._lock:
            resultado = self._elegir_resultado()
            self.doblada = False
            if resultado == VOLTEO_OK:
                self.hoja = min(self.hojas, self.hoja + 1)
            elif resultado == VOLTEO_DOS_HOJAS:
                self.hoja = min(self.hojas, self.hoja + 2)
            elif resultado == VOLTEO_DOBLADA:
                self.hoja = min(self.hojas, self.hoja + 1)
                self.doblada = True
            return resultado

    def regresar(self) -> None:
        with self._lock:
            self.hoja = max(0, self.hoja - 1)
            self.doblada = False

    def enderezar(self) -> None:
        """Lo que hace la persona al revisar la hoja."""
        with self._lock:
            self.doblada = False

    @property
    def pagina_derecha(self) -> int:
        return 2 * self.hoja + 1

    # --- imagen -----------------------------------------------------------
    def renderizar(self, ancho: int = 1296, alto: int = 972) -> np.ndarray:
        """Foto cenital del libro abierto (BGR), con texto y número de página."""
        with self._lock:
            hoja, doblada = self.hoja, self.doblada
        img = np.full((alto, ancho, 3), (60, 90, 120), np.uint8)  # atril de madera
        m = int(ancho * 0.06)
        x0, x1, y0, y1 = m, ancho - m, int(alto * 0.08), int(alto * 0.92)
        cx = ancho // 2
        cv2.rectangle(img, (x0, y0), (x1, y1), (235, 238, 240), -1)
        cv2.line(img, (cx, y0), (cx, y1), (150, 150, 150), 3)
        # Canto de las hojas pasadas: más grueso cuantas más hojas hay a la izquierda
        grosor = 2 + hoja
        for i in range(min(grosor, 40)):
            cv2.line(img, (x0 - i, y0 + 4), (x0 - i, y1 - 4), (200 - 3 * (i % 5), 205, 210), 1)
        restantes = self.hojas - hoja
        for i in range(min(2 + restantes, 40)):
            cv2.line(img, (x1 + i, y0 + 4), (x1 + i, y1 - 4), (200 - 3 * (i % 5), 205, 210), 1)

        for lado, numero in (("izq", 2 * hoja), ("der", 2 * hoja + 1)):
            px0 = x0 + 30 if lado == "izq" else cx + 30
            if numero == 0:
                continue  # contratapa en blanco
            self._escribir_pagina(img, px0, y0, cx - x0 - 60, y1 - y0, numero)

        if doblada:
            esquina = np.array([[x1, y1], [x1 - 160, y1], [x1, y1 - 160]], np.int32)
            cv2.fillPoly(img, [esquina], (180, 185, 190))
            cv2.line(img, (x1 - 160, y1), (x1, y1 - 160), (90, 90, 90), 3)
        return img

    def _escribir_pagina(self, img: np.ndarray, x: int, y: int, ancho: int, alto: int, numero: int) -> None:
        fuente = cv2.FONT_HERSHEY_SIMPLEX
        azar = random.Random(numero)
        renglon = y + 70 + azar.randint(0, 80)  # cada página con su propia diagramación
        sangria = azar.randint(0, 60)
        cv2.putText(img, f"Capitulo {1 + numero // 10}", (x + sangria, renglon), fuente, 0.9, (20, 20, 20), 2)
        renglon += 50
        while renglon < y + alto - 90:
            frase = azar.choice(_FRASES)
            for trozo in (frase[:38], frase[38:]):
                if trozo and renglon < y + alto - 90:
                    cv2.putText(img, trozo.strip(), (x, renglon), fuente, 0.55, (30, 30, 30), 1)
                    renglon += 30
        cv2.putText(img, str(numero), (x + ancho // 2, y + alto - 35), fuente, 0.8, (20, 20, 20), 2)
