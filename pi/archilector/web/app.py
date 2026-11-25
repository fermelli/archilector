"""Panel web de ArchiLector (FastAPI).

* ``/``             estado en vivo y control del documento en curso
* ``/documentos``   documentos guardados, búsqueda en el texto y descarga del PDF
* ``/etiquetar``    etiquetado de los pares antes/después para entrenar el verificador
* ``/calibracion``  parámetros de la tabla 5.5 (CFG) y pruebas de movimiento
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import asdict
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..enlace.esp32 import ErrorEnlace
from ..ia.clasificador import CATEGORIAS, NOMBRES
from ..ia.verificador import CLASES
from ..orquestador import MODOS

log = logging.getLogger(__name__)
AQUI = Path(__file__).parent

PARAMETROS_CFG = [
    ("ANG_REPOSO", "Ángulo del brazo en reposo (°)"),
    ("ANG_CONTACTO", "Ángulo con el rodillo sobre la página derecha (°)"),
    ("ANG_DEPOSITO", "Ángulo sobre la página izquierda (°)"),
    ("PASOS_ARCO", "Pasos del rodillo para arquear la hoja"),
    ("VEL_GIRO", "Velocidad de la rampa del MG996R (°/s)"),
    ("BARRA_ABIERTA", "Ángulo del SG90 con la barra abierta (°)"),
    ("BARRA_CERRADA", "Ángulo del SG90 con la barra cerrada (°)"),
    ("VEL_BARRA", "Velocidad de la barra (°/s)"),
    ("VEL_RODILLO", "Velocidad del rodillo (pasos/s)"),
    ("DIR_RODILLO", "Sentido hacia el lomo (1 o -1)"),
    ("EMBRAGUE", "Rodillos con embrague (1 deshabilita BACK)"),
    ("T_ASENTAR", "Pausa al apoyar y depositar (ms)"),
    ("PULSO_MIN", "Pulso del servo para 0° (µs)"),
    ("PULSO_MAX", "Pulso del servo para 180° (µs)"),
]
NOMBRES_CLASES = {
    "pagina_nueva": "Página nueva",
    "misma_pagina": "Misma página",
    "dos_hojas": "Dos hojas",
    "hoja_doblada": "Hoja doblada",
}


def crear_app(orquestador, db, enlace, cfg: dict) -> FastAPI:
    datos = Path(cfg["datos"]).resolve()
    datos.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="ArchiLector", docs_url=None, redoc_url=None)
    app.mount("/estaticos", StaticFiles(directory=AQUI / "estaticos"), name="estaticos")
    app.mount("/datos", StaticFiles(directory=datos), name="datos")
    plantillas = Jinja2Templates(directory=AQUI / "plantillas")

    def url_datos(ruta: str | None) -> str:
        if not ruta:
            return ""
        try:
            return "/datos/" + Path(ruta).resolve().relative_to(datos).as_posix()
        except ValueError:
            return ""

    plantillas.env.filters["url_datos"] = url_datos
    plantillas.env.globals.update(NOMBRES_CLASES=NOMBRES_CLASES, NOMBRES_CATEGORIAS=NOMBRES)

    def pagina(nombre: str, request: Request, **ctx) -> HTMLResponse:
        return plantillas.TemplateResponse(request, nombre, {"request": request, **ctx})

    # --- estado y control ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def inicio(request: Request):
        return pagina("inicio.html", request, modos=MODOS, stats=db.estadisticas_volteo())

    @app.get("/api/estado")
    def api_estado():
        e = asdict(orquestador.publico)
        e["ultima_pagina"] = url_datos(e["ultima_pagina"])
        e["ocupado"] = orquestador.ocupado
        e["esp32"] = {
            "firmware": getattr(enlace, "version_firmware", None),
            "protocolo": getattr(enlace, "version_protocolo", None),
            "caido": getattr(enlace, "caido", None),
        } if enlace else None
        e["verificador"] = orquestador.verificador.fuente
        return JSONResponse(e)

    @app.post("/control/iniciar")
    def control_iniciar(titulo: str = Form(...), libro: str = Form(...), modo: str = Form(...),
                        max_paginas: int | None = Form(None)):
        try:
            orquestador.iniciar_documento(titulo.strip() or "Documento", libro.strip() or "libro", modo,
                                          max_paginas or None)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/control/{orden}")
    def control(orden: str):
        validas = {"iniciar", "pausa", "parar", "confirmar", "siguiente", "repetir", "anterior", "terminar",
                   "volumen+", "volumen-", "ayuda"}
        if orden not in validas:
            raise HTTPException(404, "orden desconocida")
        orquestador.orden(orden)
        return {"ok": True}

    # --- documentos ------------------------------------------------------------
    @app.get("/documentos", response_class=HTMLResponse)
    def documentos(request: Request, q: str = ""):
        resultados = db.buscar(q) if q.strip() else []
        return pagina("documentos.html", request, documentos=db.documentos(), q=q, resultados=resultados)

    @app.get("/documentos/{doc_id}", response_class=HTMLResponse)
    def documento(request: Request, doc_id: int):
        d = db.documento(doc_id)
        if not d:
            raise HTTPException(404, "documento no encontrado")
        return pagina("documento.html", request, doc=d, paginas=db.paginas(doc_id), volteos=db.volteos(doc_id),
                      categorias=CATEGORIAS)

    @app.get("/documentos/{doc_id}/pdf")
    def documento_pdf(doc_id: int):
        d = db.documento(doc_id)
        if not d:
            raise HTTPException(404, "documento no encontrado")
        ruta = d["ruta_pdf"]
        if not ruta or not Path(ruta).exists():
            try:
                ruta = str(orquestador.generar_pdf(doc_id))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return FileResponse(ruta, media_type="application/pdf", filename=Path(ruta).name)

    @app.post("/paginas/{pagina_id}/categoria")
    def categoria_pagina(pagina_id: int, categoria: str = Form(...), doc_id: int = Form(...)):
        if categoria not in CATEGORIAS:
            raise HTTPException(400, "categoría desconocida")
        db.etiquetar_pagina(pagina_id, categoria)
        return RedirectResponse(f"/documentos/{doc_id}#p{pagina_id}", status_code=303)

    # --- etiquetado para entrenar ------------------------------------------------
    @app.get("/etiquetar", response_class=HTMLResponse)
    def etiquetar(request: Request, id: int | None = None):
        par = db.par(id) if id else db.par_sin_etiqueta()
        meta = {}
        if par:
            ruta_meta = Path(par["ruta"]) / "meta.json"
            if ruta_meta.exists():
                meta = json.loads(ruta_meta.read_text(encoding="utf-8"))
        return pagina("etiquetar.html", request, par=par, meta=meta, clases=CLASES, conteo=db.conteo_pares())

    @app.post("/etiquetar/{par_id}")
    def etiquetar_par(par_id: int, etiqueta: str = Form(...)):
        if etiqueta not in CLASES and etiqueta != "descartar":
            raise HTTPException(400, "clase desconocida")
        db.etiquetar_par(par_id, etiqueta)
        return RedirectResponse("/etiquetar", status_code=303)

    @app.get("/etiquetar/verificador.zip")
    def exportar_verificador():
        """Una carpeta por clase con las imágenes compuestas, más meta.csv con el
        libro (para dividir por libro) y la predicción de la regla de píxeles."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            filas = io.StringIO()
            w = csv.writer(filas)
            w.writerow(["archivo", "clase", "libro", "regla_pixeles", "prediccion"])
            for p in db.pares_etiquetados():
                if p["etiqueta"] == "descartar":
                    continue
                origen = Path(p["ruta"]) / "compuesto.jpg"
                if not origen.exists():
                    continue
                nombre = f"{p['etiqueta']}/{p['libro']}__{p['id']:05d}.jpg"
                z.write(origen, nombre)
                w.writerow([nombre, p["etiqueta"], p["libro"], p["regla_pixeles"], p["prediccion"]])
            z.writestr("meta.csv", filas.getvalue())
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition": "attachment; filename=verificador.zip"})

    @app.get("/etiquetar/clasificador.zip")
    def exportar_clasificador():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            filas = io.StringIO()
            w = csv.writer(filas)
            w.writerow(["archivo", "categoria", "libro", "prediccion_regla", "texto"])
            for p in db.paginas_etiquetadas():
                if not Path(p["ruta_imagen"]).exists():
                    continue
                nombre = f"{p['categoria_manual']}/{p['libro']}__{p['id']:05d}.jpg"
                z.write(p["ruta_imagen"], nombre)
                w.writerow([nombre, p["categoria_manual"], p["libro"], p["categoria"], (p["texto"] or "")[:500]])
            z.writestr("meta.csv", filas.getvalue())
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
                                 headers={"Content-Disposition": "attachment; filename=clasificador.zip"})

    # --- calibración -------------------------------------------------------------
    @app.get("/calibracion", response_class=HTMLResponse)
    def calibracion(request: Request, msg: str = ""):
        return pagina("calibracion.html", request, parametros=PARAMETROS_CFG, msg=msg,
                      ocupado=orquestador.ocupado, hay_enlace=enlace is not None)

    def _enviar(*cmd) -> str:
        if enlace is None:
            raise HTTPException(409, "no hay enlace con el ESP32")
        if orquestador.ocupado and cmd[0] not in ("STATUS", "STOP", "PING"):
            raise HTTPException(409, "hay un documento en curso")
        try:
            r = enlace.comando(*cmd)
            return f"{' '.join(map(str, cmd))} → {r.cmd} {','.join(r.args)} ({r.latencia * 1000:.0f} ms)"
        except ErrorEnlace as exc:
            return f"{' '.join(map(str, cmd))} → {exc}"

    @app.post("/calibracion/cfg")
    def calibracion_cfg(parametro: str = Form(...), valor: int = Form(...)):
        if parametro not in dict(PARAMETROS_CFG):
            raise HTTPException(400, "parámetro desconocido")
        return RedirectResponse(f"/calibracion?msg={quote(_enviar('CFG', parametro, valor))}", status_code=303)

    @app.post("/calibracion/comando")
    def calibracion_comando(comando: str = Form(...), argumento: str = Form("")):
        permitidos = {"PING", "STATUS", "HOME", "TURN", "RETRY", "BACK", "STOP", "LIGHT", "BEEP"}
        if comando not in permitidos:
            raise HTTPException(400, "comando no permitido")
        args = [a for a in [argumento.strip()] if a]
        return RedirectResponse(f"/calibracion?msg={quote(_enviar(comando, *args))}", status_code=303)

    return app
