"""Separación de la página izquierda y la derecha de una misma foto (búsqueda del lomo)."""
import cv2
import numpy as np

from archilector.simulacion import LibroSimulado
from archilector.vision import preproceso


def _pliegue(libro):
    """Columna real del pliegue del simulador: la línea gris del centro."""
    gris = cv2.cvtColor(libro, cv2.COLOR_BGR2GRAY).mean(axis=0)
    a, b = int(len(gris) * 0.35), int(len(gris) * 0.65)
    return a + int(np.argmin(gris[a:b]))


def test_lomo_cae_en_el_pliegue():
    libro = preproceso.corregir(LibroSimulado().renderizar()).libro
    assert abs(preproceso.lomo(libro) - _pliegue(libro)) <= 3


def test_lomo_corrido_se_sigue():
    libro = preproceso.corregir(LibroSimulado().renderizar()).libro
    corrido = np.roll(libro, int(libro.shape[1] * 0.1), axis=1)
    assert abs(preproceso.lomo(corrido) - _pliegue(corrido)) <= 3


def test_sin_lomo_visible_corta_al_medio():
    assert preproceso.lomo(np.full((100, 200, 3), 230, np.uint8)) == 100


def test_ambas_devuelve_las_dos_paginas():
    r = preproceso.corregir(LibroSimulado().renderizar(), "ambas")
    ancho = r.libro.shape[1]
    assert r.izquierda is not None
    # Cada una es cerca de la mitad, con el margen del 2 % sobre el pliegue
    for pagina in (r.izquierda, r.pagina):
        assert 0.4 * ancho < pagina.shape[1] < 0.6 * ancho
    assert preproceso.corregir(LibroSimulado().renderizar()).izquierda is None
