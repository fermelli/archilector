"""Configuración: valores por defecto + ``config.yaml`` (se mezclan por secciones)."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

POR_DEFECTO: dict = {
    "datos": "datos",  # carpeta de imágenes, PDF, pares y base de datos
    "enlace": {
        "puerto": "/dev/archilector-esp32",  # regla udev, sección 6.4; "sim://" para simular
        "latido": True,
    },
    "camara": {
        "fuente": "auto",  # auto | pi | webcam[:<n>|:<ruta>] | sim | carpeta:<ruta>
        "pi": {"ancho": 2592, "alto": 1944},  # cámara V1.3 por CSI
        "webcam": {"ancho": 1280, "alto": 720, "asentar_s": 0.5},  # Redragon GW600: 720p
        # Canto de las hojas pasadas por cámara, si quedan encuadradas distinto:
        # {"pi": [...], "webcam": [...]} (si falta, se usa vision.region_canto)
        "region_canto": {},
        "luz_captura": 100,  # % de la tira LED al fotografiar
        "luz_reposo": 30,
        "espera_luz": 0.3,  # s para que la luz se estabilice
    },
    "vision": {
        "lado_pagina": "derecha",  # una cara por volteo (alcance 1.4)
        # Canto de las hojas pasadas en la foto (fracciones x0, y0, x1, y1); ajustar tras el montaje
        "region_canto": [0.0, 0.2, 0.15, 0.8],
        "paso_numeracion": 2,  # la página derecha avanza de 2 en 2 por hoja
    },
    "ocr": {"activo": True},
    "ia": {
        "modelo_verificador": "modelos/verificador.tflite",
        "modelo_clasificador": "modelos/clasificador.tflite",
        "umbral_confianza": 0.80,  # sección 4.5
        "umbral_regla_pixeles": 0.03,  # calibrar con los pares reales
        "guardar_pares": True,
    },
    "volteo": {
        "reintentos": 2,  # tabla 6 y figura 8.2
        "pasos_extra_retry": 40,
    },
    "lectura": {
        "avance_automatico": True,  # al terminar de leer pide el volteo
        "saltar_blancas": True,
    },
    "voz": {
        "activa": True,
        "piper_modelo": "modelos/voz/es_ES-davefx-medium.onnx",
        "vosk_modelo": "modelos/vosk-model-small-es-0.42",
        "volumen": 70,
        "velocidad": 1.0,  # length_scale de Piper: >1 más lento
        "escuchar": True,
        # Dispositivo ALSA del micrófono (arecord -L), p. ej. "plughw:CARD=Device,DEV=0".
        # Fijarlo si hay una webcam con micrófono: con dos micrófonos USB el orden puede cambiar.
        "microfono": None,
    },
    "web": {"host": "0.0.0.0", "puerto": 8000},
    "simulacion": {"hojas": 20, "escala_tiempo": 1.0, "prob_fallas": {}},
}


def _mezclar(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mezclar(base[k], v)
        else:
            base[k] = v
    return base


def cargar(ruta: str | Path | None = None) -> dict:
    cfg = copy.deepcopy(POR_DEFECTO)
    if ruta and Path(ruta).exists():
        with open(ruta, encoding="utf-8") as f:
            _mezclar(cfg, yaml.safe_load(f) or {})
    return cfg
