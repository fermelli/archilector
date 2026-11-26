"""Corrección de la foto del libro abierto (sección 3.4 del Diseño, OpenCV).

1. Detecta el contorno del libro (la zona clara más grande) y corrige la perspectiva.
2. Mejora el contraste con CLAHE sobre la luminancia.
3. Busca el lomo y separa la página izquierda, la derecha o ambas.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FotoCorregida:
    libro: np.ndarray    # libro abierto completo, enderezado (BGR)
    pagina: np.ndarray   # página capturada (BGR, contraste mejorado); con «ambas», la derecha
    nitidez: float       # varianza del Laplaciano: métrica de calidad
    contorno_hallado: bool
    izquierda: np.ndarray | None = None  # solo con «ambas»


def _ordenar_esquinas(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def detectar_libro(img: np.ndarray, area_minima: float = 0.25) -> np.ndarray | None:
    """Devuelve las 4 esquinas del libro (sup-izq, sup-der, inf-der, inf-izq) o None."""
    escala = 800 / max(img.shape[:2])
    chica = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    gris = cv2.GaussianBlur(cv2.cvtColor(chica, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, binaria = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binaria = cv2.morphologyEx(binaria, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contornos, _ = cv2.findContours(binaria, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        return None
    mayor = max(contornos, key=cv2.contourArea)
    if cv2.contourArea(mayor) < area_minima * chica.shape[0] * chica.shape[1]:
        return None
    aprox = cv2.approxPolyDP(mayor, 0.02 * cv2.arcLength(mayor, True), True)
    if len(aprox) != 4:
        x, y, w, h = cv2.boundingRect(mayor)
        aprox = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
    return _ordenar_esquinas(aprox / escala)


def enderezar(img: np.ndarray, esquinas: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = esquinas
    ancho = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    alto = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    destino = np.array([[0, 0], [ancho - 1, 0], [ancho - 1, alto - 1], [0, alto - 1]], np.float32)
    m = cv2.getPerspectiveTransform(esquinas, destino)
    return cv2.warpPerspective(img, m, (ancho, alto), flags=cv2.INTER_CUBIC)


def mejorar_contraste(img: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def nitidez(img: np.ndarray) -> float:
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(gris, cv2.CV_64F).var())


def lomo(libro: np.ndarray) -> int:
    """Columna del lomo: la franja más oscura (su sombra) en el 30 % central; si no se nota, la mitad."""
    gris = cv2.cvtColor(libro, cv2.COLOR_BGR2GRAY) if libro.ndim == 3 else libro
    ancho = gris.shape[1]
    a, b = int(ancho * 0.35), int(ancho * 0.65)
    k = max(3, ancho // 200)
    perfil = np.convolve(gris[:, a:b].mean(axis=0), np.ones(k) / k, mode="same")[k:-k]
    i = int(np.argmin(perfil))
    return a + k + i if perfil[i] < 0.9 * np.median(perfil) else ancho // 2


def separar_pagina(libro: np.ndarray, lado: str = "derecha") -> np.ndarray:
    """Corta en el lomo, con un margen del 2 % para no comerse letras junto al pliegue."""
    c, m = lomo(libro), int(libro.shape[1] * 0.02)
    return libro[:, max(0, c - m):] if lado == "derecha" else libro[:, :c + m]


def corregir(img: np.ndarray, lado: str = "derecha") -> FotoCorregida:
    """``lado``: derecha, izquierda o ambas (la derecha queda en ``pagina``)."""
    esquinas = detectar_libro(img)
    libro = enderezar(img, esquinas) if esquinas is not None else img
    pagina = mejorar_contraste(separar_pagina(libro, "izquierda" if lado == "izquierda" else "derecha"))
    izquierda = mejorar_contraste(separar_pagina(libro, "izquierda")) if lado == "ambas" else None
    return FotoCorregida(libro=libro, pagina=pagina, nitidez=nitidez(pagina), contorno_hallado=esquinas is not None,
                         izquierda=izquierda)
