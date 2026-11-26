#!/usr/bin/env python3
"""Panel de pruebas de ArchiLector: camara en vivo + OCR + voz + metricas.

Solo para la Raspberry Pi y para la red local del laboratorio: NO tiene autenticacion
y NO debe exponerse a internet. Ocupa la camara elegida (la CSI o la webcam USB), asi que esa
camara no puede estar a la vez en ``python -m archilector ejecutar``.

Captura la pagina a 2592x1944, la lee con Tesseract (idioma ``spa``, requiere
``sudo apt install tesseract-ocr tesseract-ocr-spa``), con RapidOCR o con Claude (clave en ``pi/.env``), la sintetiza con Piper y
re-transcribe el audio con Vosk para medir el error de palabras (WER). El audio suena en el
navegador de quien abre la pagina, no en la Pi.

    cd pi && .venv/bin/python herramientas/panel_pruebas.py [--puerto 8889] [--camara pi|webcam|sim]

Con ``--camara sim`` usa el libro simulado de ArchiLector: sirve para ver el panel en un PC, sin la Pi.
"""
import argparse
import difflib
import glob
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor
from http import server
from socketserver import ThreadingMixIn

import cv2
import numpy as np
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # carpeta pi/
PIPER = f"{RAIZ}/modelos/voz/es_ES-davefx-medium.onnx"
VOSK = f"{RAIZ}/modelos/vosk-model-small-es-0.42"
sys.path.insert(0, RAIZ)

ap = argparse.ArgumentParser(description="Panel de pruebas de ArchiLector (solo LAN, sin autenticacion)")
ap.add_argument("--puerto", type=int, default=8889)
ap.add_argument("--camara", choices=["pi", "webcam", "sim"], default="pi",
                help="CSI (2592x1944), webcam USB (1280x720) o libro simulado (para un PC sin cámara)")
args = ap.parse_args()
if os.path.exists(f"{RAIZ}/.env"):  # ANTHROPIC_API_KEY para leer con Claude
    for _l in open(f"{RAIZ}/.env", encoding="utf-8").read().splitlines():
        _k, _, _v = _l.partition("=")
        if _k.strip() and not _k.startswith("#"):
            os.environ.setdefault(_k.strip(), _v.strip())

# ----------------------------------------------------------------------------- camara
class Salida(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.frame = None
        self.cond = threading.Condition()
        self.mirando = 0  # navegadores con el video en vivo abierto

    def write(self, buf):
        with self.cond:
            self.frame = bytes(buf)
            self.cond.notify_all()
        return len(buf)


class Webcam:
    """Webcam USB con OpenCV: misma interfaz que Picamera2 para el panel (capture_array y video en vivo)."""

    def __init__(self, salida, ancho=1280, alto=720):
        rutas = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
        if not rutas:  # sin esto abriría /dev/video0, que es la CSI de ArchiLector
            raise SystemExit("no hay webcam USB conectada")
        self.cap = cv2.VideoCapture(rutas[0], cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise SystemExit(f"no se pudo abrir la webcam {rutas[0]} (¿la usa ArchiLector?)")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, ancho)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, alto)
        self.ultimo, self.salida = None, salida
        print(f"Webcam {rutas[0]}", flush=True)
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self):
        enviado = 0.0
        while True:
            ok, f = self.cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            self.ultimo = f
            # Comprimir el video cuesta CPU: solo si alguien lo mira, y a 10 cuadros por segundo
            if self.salida.mirando and time.monotonic() - enviado >= 0.1:
                enviado = time.monotonic()
                self.salida.write(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes())

    def capture_array(self, _nombre="main"):
        while self.ultimo is None:
            time.sleep(0.05)
        return self.ultimo.copy()


class CamSimulada:
    """Libro simulado de ArchiLector como cámara: pasa una hoja cada pocos segundos."""

    def __init__(self, salida):
        from archilector.simulacion import LibroSimulado

        self.libro, self.salida = LibroSimulado(hojas=40), salida
        threading.Thread(target=self._emitir, daemon=True).start()

    def _emitir(self):
        n = 0
        while True:
            f = self.libro.renderizar()
            self.salida.write(cv2.imencode(".jpg", cv2.resize(f, None, fx=0.5, fy=0.5), [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes())
            n += 1
            if n % 40 == 0:
                self.libro.voltear()
            time.sleep(0.2)

    def capture_array(self, _nombre="main"):
        return self.libro.renderizar()


salida = Salida()
if args.camara == "webcam":
    cam, RES = Webcam(salida), "1280×720 (webcam)"
elif args.camara == "sim":
    cam, RES = CamSimulada(salida), "libro simulado"
else:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput

    cam, RES = Picamera2(), "2592×1944 (CSI)"
    cam.configure(cam.create_video_configuration(
        main={"size": (2592, 1944), "format": "RGB888"},
        lores={"size": (1280, 960), "format": "YUV420"},
        controls={"FrameRate": 15}, buffer_count=4))
    cam.start_recording(MJPEGEncoder(), FileOutput(salida), name="lores")

# ----------------------------------------------------------------------------- modelos (carga diferida)
_m = {}
_m_lock = threading.Lock()


def piper():
    with _m_lock:
        if "piper" not in _m:
            from piper import PiperVoice
            _m["piper"] = PiperVoice.load(PIPER)
        return _m["piper"]


def vosk_modelo():
    with _m_lock:
        if "vosk" not in _m:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            _m["vosk"] = Model(VOSK)
        return _m["vosk"]


def rapid():
    with _m_lock:
        if "rapid" not in _m:
            from archilector.vision.ocr import OCR
            _m["rapid"] = OCR()
        return _m["rapid"]


class _SinRespaldo:
    def leer(self, pagina):
        m = _m["claude"].motivo if _m["claude"].hay_clave else "falta ANTHROPIC_API_KEY en pi/.env"
        raise RuntimeError(f"Claude no leyó la página: {m}")


def claude(modelo):
    from archilector.vision.ocr_claude import MODELOS, OCRClaude

    with _m_lock:
        _m.setdefault("claude", OCRClaude(_SinRespaldo()))
    _m["claude"].modelo = modelo if modelo in MODELOS else MODELOS[0]
    return _m["claude"]


def precargar():
    try:
        piper(); vosk_modelo()
    except Exception as e:  # noqa: BLE001
        print("precarga:", e, flush=True)


# ----------------------------------------------------------------------------- OCR y texto
def ocr_tesseract(pagina):
    g = cv2.cvtColor(pagina, cv2.COLOR_BGR2GRAY)
    fondo = cv2.medianBlur(cv2.dilate(g, np.ones((25, 25), np.uint8)), 51)
    n = cv2.divide(g, fondo, scale=255)  # normaliza la luz
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        cv2.imwrite(f.name, n)
        out = subprocess.run(["tesseract", f.name, "-", "-l", "spa", "--psm", "6"],
                             capture_output=True, text=True, timeout=180).stdout
    return out, numero_pagina(out)


def numero_pagina(texto):
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    for l in lineas[-3:] + lineas[:2]:
        m = re.match(r"^\W*(\d{1,4})\W*$", l)
        if m:
            return int(m.group(1))
    return None


def limpiar_para_voz(texto):
    lineas = [l for l in texto.splitlines() if not re.match(r"^[\W\d]{1,8}$", l.strip())]  # numero de pagina, rayas
    t = "\n".join(lineas)
    t = re.sub(r"-\s*\n\s*", "", t)          # palabras cortadas al final de linea
    t = re.sub(r"\n\s*\n", " ¶ ", t)           # parrafos
    t = re.sub(r"\s*\n\s*", " ", t)
    t = t.replace(" ¶ ", "\n\n")
    return re.sub(r"[ \t]+", " ", t).strip()


def palabras(s):
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return s.split()


def wer(ref, hip):
    r, h = ref, hip
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / max(1, len(r))


def sin_acentos(s):
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


# ----------------------------------------------------------------------------- trabajo
def con_lomo(libro, col):
    """Copia del libro con una línea roja donde se dividió."""
    vista = libro.copy()
    cv2.line(vista, (col, 0), (col, vista.shape[0]), (40, 40, 220), max(2, vista.shape[1] // 250))
    return vista


def guardar_vista(vista):
    esc = 900 / max(vista.shape[:2])
    archivos["imagen"] = cv2.imencode(".jpg", cv2.resize(vista, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA),
                                      [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


estado = {"corriendo": False, "pasos": [], "fase": "", "resultado": None, "error": None}
archivos = {"imagen": b"", "audio": b""}
job_lock = threading.Lock()


def paso(nombre, t0):
    estado["pasos"].append({"nombre": nombre, "seg": round(time.time() - t0, 2)})


def trabajo(p):
    try:
        res = {}
        t = time.time(); estado["fase"] = f"Capturando foto {RES}"
        frame = cam.capture_array("main")  # BGR
        paso("Captura", t)

        t = time.time(); estado["fase"] = "Preparando la página"
        h, w = frame.shape[:2]
        if p["modo"] == "ambas":  # como ArchiLector: las dos páginas de la misma foto, cortadas en el lomo
            from archilector.vision.preproceso import corregir, lomo
            f = corregir(frame, "ambas")
            paginas, vista = [f.izquierda, f.pagina], con_lomo(f.libro, lomo(f.libro))
            res["contorno_hallado"] = f.contorno_hallado
        elif p["modo"] == "proyecto":
            from archilector.vision.preproceso import corregir
            f = corregir(frame)
            pagina = f.pagina
            res["contorno_hallado"] = f.contorno_hallado
        elif p["modo"] == "manual":
            x0, y0, x1, y1 = (float(p[k]) / 100 for k in ("x0", "y0", "x1", "y1"))
            pagina = frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        else:
            pagina = frame
        if p["modo"] != "ambas":
            paginas, vista = [pagina], pagina
        guardar_vista(vista)
        res["nitidez"] = round(min(float(cv2.Laplacian(cv2.cvtColor(g, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
                                   for g in paginas), 1)
        paso("Preparar página", t)

        t = time.time(); estado["fase"] = "Leyendo el texto (OCR %s)" % p["ocr"]
        def leer(pg):
            if p["ocr"] == "tesseract":
                return ocr_tesseract(pg)
            r = (claude(p["ocr"]) if p["ocr"].startswith("claude") else rapid()).leer(pg)
            return r.texto, r.numero_pagina

        claude_divide = p["modo"] == "ambas" and p.get("division") == "claude"
        if claude_divide:  # Claude ubica el lomo y lee las dos páginas en una sola llamada
            estado["fase"] = "Claude divide el libro y lee las dos páginas"
            cl = claude(p["ocr"])
            pliego = cl.leer_pliego(f.libro)
            if pliego is None:  # como ArchiLector: se divide en la Pi y se lee cada página
                claude_divide = False
                estado["aviso"] = ("Claude no dividió el libro: " + (cl.motivo if cl.hay_clave else "falta la clave")
                                   + ". Se dividió en la Pi, buscando el lomo.")
        if claude_divide:
            col, *resultados = pliego
            guardar_vista(con_lomo(f.libro, col))
            lecturas = [(r.texto, r.numero_pagina) for r in resultados]
            res["lomo_claude"], res["lomo_local"] = round(col / f.libro.shape[1], 3), round(lomo(f.libro) / f.libro.shape[1], 3)
        else:
            with ThreadPoolExecutor(2 if p["ocr"].startswith("claude") else 1) as ex:  # Claude: las dos a la vez
                lecturas = list(ex.map(leer, paginas))
        texto, num = "\n\n".join(t for t, _ in lecturas), lecturas[-1][1]  # la voz lee izquierda y luego derecha
        lados = ["izquierda", "derecha"] if len(lecturas) == 2 else [""]
        res["paginas"] = [{"lado": l, "texto": tx, "numero": n} for l, (tx, n) in zip(lados, lecturas)]
        if len(lecturas) == 2:
            res["numero_izquierda"] = lecturas[0][1]
        res["ocr_seg"] = round(time.time() - t, 2)
        paso("Claude divide y lee · " + claude(p["ocr"]).modelo if claude_divide else "OCR " + p["ocr"], t)
        res["ocr_texto"] = texto
        res["numero_pagina"] = num
        estado["lectura"] = {"paginas": res["paginas"]}  # se muestra ya, sin esperar a la voz

        voz = limpiar_para_voz(texto)
        res["texto_voz"] = voz
        pal = palabras(voz)
        res["n_palabras"] = len(pal)
        if p.get("referencia", "").strip():
            res["similitud_ocr"] = round(difflib.SequenceMatcher(None, sin_acentos(p["referencia"]), sin_acentos(texto),
                                                                  autojunk=False).ratio(), 3)
            res["wer_ocr"] = round(wer(palabras(p["referencia"]), pal), 3)
        if not pal:
            raise RuntimeError("El OCR no encontró texto en la imagen.")

        t = time.time(); estado["fase"] = "Sintetizando voz (Piper, velocidad %.2f)" % p["vel"]
        from piper import SynthesisConfig
        v = piper()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wv:
            v.synthesize_wav(voz, wv, syn_config=SynthesisConfig(length_scale=p["vel"]))
        sint = time.time() - t
        paso("Síntesis de voz", t)
        archivos["audio"] = buf.getvalue()
        buf.seek(0)
        with wave.open(buf) as wv:
            sr, nfr = wv.getframerate(), wv.getnframes()
            audio = np.frombuffer(wv.readframes(nfr), dtype=np.int16)
        dur = nfr / sr
        res.update(audio_seg=round(dur, 1), sintesis_seg=round(sint, 2), factor_tiempo_real=round(sint / dur, 2),
                   palabras_por_min=round(len(pal) / dur * 60))

        t = time.time(); estado["fase"] = "Verificando la voz (Vosk re-transcribe el audio)"
        from vosk import KaldiRecognizer
        x = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / sr)), np.arange(len(audio)),
                      audio.astype(np.float32)).astype(np.int16).tobytes()
        rec = KaldiRecognizer(vosk_modelo(), 16000)
        for i in range(0, len(x), 8000):
            rec.AcceptWaveform(x[i:i + 8000])
        oido = json.loads(rec.FinalResult()).get("text", "")
        paso("Verificar con Vosk", t)
        res["vosk_texto"] = oido
        res["wer_voz"] = round(wer(pal, oido.split()), 3)

        res["total_seg"] = round(sum(s["seg"] for s in estado["pasos"]), 1)
        res["motor"] = claude(p["ocr"]).modelo + " (divide y lee)" if claude_divide else p["ocr"]
        res["vel"] = p["vel"]
        estado["resultado"] = res
        estado["fase"] = "Listo"
    except Exception as e:  # noqa: BLE001
        estado["error"] = f"{type(e).__name__}: {e}"
        estado["fase"] = "Error"
    finally:
        estado["corriendo"] = False
        job_lock.release()


# ----------------------------------------------------------------------------- pagina
def pagina_html() -> bytes:
    """La página está en panel_pruebas.html (se lee en cada pedido: editar y recargar)."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_pruebas.html"), "rb") as f:
        return f.read()

# ----------------------------------------------------------------------------- servidor
class H(server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, cuerpo, tipo, codigo=200):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = self.path.split("?")[0]
        if ruta == "/s.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            salida.mirando += 1
            try:
                while True:
                    with salida.cond:
                        salida.cond.wait()
                        f = salida.frame
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(f) + f + b"\r\n")
            except Exception:  # noqa: BLE001
                pass
            finally:
                salida.mirando -= 1
        elif ruta == "/api/estado":
            self._send(json.dumps(estado).encode(), "application/json")
        elif ruta == "/api/info":
            try:
                mic = "card" in subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
            except OSError:  # PC sin ALSA
                mic = False
            self._send(json.dumps({"mic": mic, "modelos": "piper" in _m and "vosk" in _m, "camara": RES,
                                   "clave": bool(os.environ.get("ANTHROPIC_API_KEY"))}).encode(), "application/json")
        elif ruta == "/estaticos/estilo.css":  # el mismo diseño que el panel de ArchiLector
            with open(f"{RAIZ}/archilector/web/estaticos/estilo.css", "rb") as f:
                self._send(f.read(), "text/css; charset=utf-8")
        elif ruta == "/api/imagen.jpg":
            self._send(archivos["imagen"], "image/jpeg")
        elif ruta == "/api/audio.wav":
            self._send(archivos["audio"], "audio/wav")
        else:
            self._send(pagina_html(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/leer":
            return self._send(b"{}", "application/json", 404)
        p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not job_lock.acquire(blocking=False):
            return self._send(json.dumps({"ok": False, "error": "Ya hay una prueba en curso."}).encode(), "application/json")
        estado.update(corriendo=True, pasos=[], fase="Iniciando", resultado=None, error=None, lectura=None, aviso=None)
        threading.Thread(target=trabajo, args=(p,), daemon=True).start()
        self._send(json.dumps({"ok": True}).encode(), "application/json")


class S(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=precargar, daemon=True).start()
    print(f"Panel de pruebas en http://0.0.0.0:{args.puerto}  (solo red local, sin autenticacion)", flush=True)
    S(("0.0.0.0", args.puerto), H).serve_forever()
