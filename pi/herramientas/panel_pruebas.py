#!/usr/bin/env python3
"""Panel de pruebas de ArchiLector: camara en vivo + OCR + voz + metricas.

Solo para la Raspberry Pi y para la red local del laboratorio: NO tiene autenticacion
y NO debe exponerse a internet. Ocupa la camara elegida (la CSI o la webcam USB), asi que esa
camara no puede estar a la vez en ``python -m archilector ejecutar``.

Captura la pagina a 2592x1944, la lee con Tesseract (idioma ``spa``, requiere
``sudo apt install tesseract-ocr tesseract-ocr-spa``) o con RapidOCR, la sintetiza con Piper y
re-transcribe el audio con Vosk para medir el error de palabras (WER). El audio suena en el
navegador de quien abre la pagina, no en la Pi.

    cd pi && .venv/bin/python herramientas/panel_pruebas.py [--puerto 8889] [--camara pi|webcam]
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
ap.add_argument("--camara", choices=["pi", "webcam"], default="pi", help="CSI (2592x1944) o webcam USB (1280x720)")
args = ap.parse_args()

# ----------------------------------------------------------------------------- camara
class Salida(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = bytes(buf)
            self.cond.notify_all()
        return len(buf)


class Webcam:
    """Webcam USB con OpenCV: misma interfaz que Picamera2 para el panel (capture_array y video en vivo)."""

    def __init__(self, salida, ancho=1280, alto=720):
        rutas = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0")) or [0]
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
        while True:
            ok, f = self.cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            self.ultimo = f
            self.salida.write(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes())

    def capture_array(self, _nombre="main"):
        while self.ultimo is None:
            time.sleep(0.05)
        return self.ultimo.copy()


salida = Salida()
if args.camara == "webcam":
    cam, RES = Webcam(salida), "1280×720 (webcam)"
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
        if p["modo"] == "proyecto":
            from archilector.vision.preproceso import corregir
            f = corregir(frame)
            pagina = f.pagina
            res["contorno_hallado"] = f.contorno_hallado
        elif p["modo"] == "manual":
            x0, y0, x1, y1 = (float(p[k]) / 100 for k in ("x0", "y0", "x1", "y1"))
            pagina = frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        else:
            pagina = frame
        ok, jpg = cv2.imencode(".jpg", cv2.resize(pagina, None, fx=900 / max(pagina.shape[:2]), fy=900 / max(pagina.shape[:2]),
                                                    interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 80])
        archivos["imagen"] = jpg.tobytes()
        res["nitidez"] = round(float(cv2.Laplacian(cv2.cvtColor(pagina, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()), 1)
        paso("Preparar página", t)

        t = time.time(); estado["fase"] = "Leyendo el texto (OCR %s)" % p["ocr"]
        if p["ocr"] == "tesseract":
            texto, num = ocr_tesseract(pagina)
        else:
            r = rapid().leer(pagina)
            texto, num = r.texto, r.numero_pagina
        res["ocr_seg"] = round(time.time() - t, 2)
        paso("OCR " + p["ocr"], t)
        res["ocr_texto"] = texto
        res["numero_pagina"] = num

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
        res["motor"] = p["ocr"]
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
HTML = r"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ArchiLector · Pruebas</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1b1f24;--mut:#667085;--bd:#d9dde3;--ac:#1f6feb;--ok:#1a7f37;--ma:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c23;--tx:#e6e9ee;--mut:#9aa4b2;--bd:#2a313b;--ac:#58a6ff;--ok:#3fb950;--ma:#e3a008}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--bd);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h1{font-size:18px;margin:0}small,.mut{color:var(--mut)}
main{display:grid;grid-template-columns:minmax(300px,1fr) minmax(320px,1fr);gap:14px;padding:14px;max-width:1500px;margin:auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 8px}
img.live,img.pg{width:100%;border-radius:6px;background:#000;display:block}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}
label{font-size:13px;color:var(--mut)}select,input,textarea{background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;font:inherit}
input[type=number]{width:64px}textarea{width:100%;min-height:60px}
button{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:10px 16px;font:600 15px system-ui;cursor:pointer}button:disabled{opacity:.5;cursor:wait}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.k{border:1px solid var(--bd);border-radius:8px;padding:8px}.k b{display:block;font-size:20px}.k span{font-size:12px;color:var(--mut)}
.pasos div{display:flex;justify-content:space-between;border-bottom:1px dashed var(--bd);padding:3px 0}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:8px;margin:6px 0;max-height:260px;overflow:auto;font:13px/1.4 ui-monospace,monospace}
.ok{color:var(--ok)}.ma{color:var(--ma)}audio{width:100%;margin-top:6px}
</style></head><body>
<header><h1>ArchiLector · Panel de pruebas</h1><small id="info">…</small></header>
<main>
<section>
 <div class="card"><h2>Cámara en vivo</h2><img class="live" src="/s.mjpg" alt="cámara">
  <div class="row mut">La foto para el OCR se toma a 2592×1944; aquí ves una vista de 1280×960.</div></div>
 <div class="card" style="margin-top:14px"><h2>Ajustes</h2>
  <div class="row"><label>OCR <select id="ocr"><option value="tesseract">Tesseract (español)</option><option value="rapidocr">RapidOCR (PP-OCRv4)</option></select></label>
   <label>Velocidad de voz <input id="vel" type="range" min="0.7" max="1.5" step="0.05" value="1.0" oninput="velv.textContent=this.value"> <b id="velv">1.0</b></label></div>
  <div class="row"><label>Página <select id="modo" onchange="man.style.display=this.value=='manual'?'flex':'none'"><option value="manual">Recorte manual</option><option value="proyecto">Preproceso del proyecto</option><option value="ninguno">Imagen completa</option></select></label></div>
  <div class="row" id="man"><label>izq % <input type="number" id="x0" value="23"></label><label>arriba % <input type="number" id="y0" value="0"></label><label>der % <input type="number" id="x1" value="78"></label><label>abajo % <input type="number" id="y1" value="97"></label></div>
  <label>Texto correcto de la página (opcional, para medir la precisión del OCR)</label><textarea id="ref" placeholder="Pega aquí el texto real de la página…"></textarea>
  <div class="row"><button id="go" onclick="lanzar()">📷 Capturar y leer en voz alta</button></div>
 </div>
</section>
<section>
 <div class="card"><h2>Progreso <span id="fase" class="mut"></span></h2><div class="pasos" id="pasos"><span class="mut">Aún sin ejecutar.</span></div><div id="err" class="ma"></div></div>
 <div class="card" style="margin-top:14px"><h2>Métricas</h2><div class="kpis" id="kpis"><span class="mut">—</span></div></div>
 <div class="card" style="margin-top:14px"><h2>Voz (suena en esta computadora)</h2><audio id="aud" controls></audio><div class="mut" id="vosk"></div></div>
 <div class="card" style="margin-top:14px"><h2>Imagen que lee el OCR</h2><img class="pg" id="pg" alt=""></div>
 <div class="card" style="margin-top:14px"><h2>Texto leído por el OCR</h2><pre id="txt">—</pre></div>
</section></main>
<script>
const $=id=>document.getElementById(id);let timer=null,lastRes=null;
fetch('/api/info').then(r=>r.json()).then(i=>{$('info').textContent='Micrófono en la Pi: '+(i.mic?'detectado':'no detectado')+' · Piper y Vosk '+(i.modelos?'cargados':'cargando…')});
async function lanzar(){
 const b={ocr:$('ocr').value,vel:+$('vel').value,modo:$('modo').value,x0:$('x0').value,y0:$('y0').value,x1:$('x1').value,y1:$('y1').value,referencia:$('ref').value};
 const r=await fetch('/api/leer',{method:'POST',body:JSON.stringify(b)});const j=await r.json();
 if(!j.ok){$('err').textContent=j.error;return}
 $('go').disabled=true;$('err').textContent='';$('aud').pause();lastRes=null;timer=setInterval(poll,400);poll();
}
async function poll(){
 const e=await (await fetch('/api/estado')).json();
 $('fase').textContent=e.corriendo?'· '+e.fase:'· '+e.fase;
 $('pasos').innerHTML=e.pasos.map(p=>`<div><span>${p.nombre}</span><b>${p.seg.toFixed(1)} s</b></div>`).join('')||'<span class="mut">…</span>';
 if(!e.corriendo){clearInterval(timer);$('go').disabled=false;
  if(e.error){$('err').textContent=e.error;return}
  if(e.resultado&&e.resultado!==lastRes){lastRes=e.resultado;mostrar(e.resultado)}}
}
function k(v,l,cls){return `<div class="k"><b class="${cls||''}">${v}</b><span>${l}</span></div>`}
function mostrar(r){
 const q=t=>Date.now();
 $('pg').src='/api/imagen.jpg?'+q();$('aud').src='/api/audio.wav?'+q();$('aud').play().catch(()=>{});
 $('txt').textContent=r.ocr_texto;
 let h=k(r.total_seg+' s','tiempo total del ciclo')+k(r.ocr_seg+' s','OCR ('+r.motor+')')+k(r.numero_pagina??'—','número de página leído')+k(r.n_palabras,'palabras')
  +k(r.audio_seg+' s','duración del audio')+k(r.sintesis_seg+' s','síntesis de voz')+k(r.factor_tiempo_real+'×','factor tiempo real (<1 = más rápido que hablar)',r.factor_tiempo_real<1?'ok':'ma')
  +k(r.palabras_por_min,'palabras por minuto (voz)')+k((r.wer_voz*100).toFixed(1)+' %','error de palabras de la voz (Vosk)',r.wer_voz<.15?'ok':'ma')+k(r.nitidez,'nitidez (Laplaciano)');
 if(r.similitud_ocr!==undefined)h+=k((r.similitud_ocr*100).toFixed(1)+' %','similitud del OCR con tu texto')+k((r.wer_ocr*100).toFixed(1)+' %','error de palabras del OCR',r.wer_ocr<.1?'ok':'ma');
 $('kpis').innerHTML=h;$('vosk').textContent='Vosk entendió: '+r.vosk_texto;
}
</script></body></html>"""

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
            try:
                while True:
                    with salida.cond:
                        salida.cond.wait()
                        f = salida.frame
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(f) + f + b"\r\n")
            except Exception:  # noqa: BLE001
                pass
        elif ruta == "/api/estado":
            self._send(json.dumps(estado).encode(), "application/json")
        elif ruta == "/api/info":
            mic = "card" in subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
            self._send(json.dumps({"mic": mic, "modelos": "piper" in _m and "vosk" in _m}).encode(), "application/json")
        elif ruta == "/api/imagen.jpg":
            self._send(archivos["imagen"], "image/jpeg")
        elif ruta == "/api/audio.wav":
            self._send(archivos["audio"], "audio/wav")
        else:
            self._send(HTML.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/leer":
            return self._send(b"{}", "application/json", 404)
        p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not job_lock.acquire(blocking=False):
            return self._send(json.dumps({"ok": False, "error": "Ya hay una prueba en curso."}).encode(), "application/json")
        estado.update(corriendo=True, pasos=[], fase="Iniciando", resultado=None, error=None)
        threading.Thread(target=trabajo, args=(p,), daemon=True).start()
        self._send(json.dumps({"ok": True}).encode(), "application/json")


class S(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=precargar, daemon=True).start()
    print(f"Panel de pruebas en http://0.0.0.0:{args.puerto}  (solo red local, sin autenticacion)", flush=True)
    S(("0.0.0.0", args.puerto), H).serve_forever()
