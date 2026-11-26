"""Panel web de ArchiLector (FastAPI).

* ``/``             estado en vivo y control del documento en curso
* ``/documentos``   documentos guardados, búsqueda en el texto y descarga del PDF
* ``/etiquetar``    etiquetado de los pares antes/después para entrenar el verificador
* ``/calibracion``  parámetros de la tabla 5.5 (CFG), pruebas de movimiento y cámara activa
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

import cv2
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..enlace import protocolo as P
from ..enlace.esp32 import ErrorEnlace, RechazoESP32
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
    ("BRAZO_360", "Brazo con servo de 360° usado como de 180° (1 sí, 0 no)"),
    ("QUIETO_BRAZO", "Señal con la que el servo de 360° no gira (°)"),
    ("VEL_360", "Velocidad de giro del servo de 360° (° de señal desde el quieto)"),
    ("MS90_MAS", "ms para girar 90° hacia ángulos mayores"),
    ("MS90_MENOS", "ms para girar 90° hacia ángulos menores"),
    ("DIR_BRAZO", "Lado de la señal que hace crecer el ángulo (1 o -1)"),
    ("MS_ARRANQUE", "ms que el servo de 360° tarda en empezar a girar"),
    ("MS_TOPE", "ms que el brazo de 360° empuja contra la hoja al apoyar (0 = no)"),
    ("PRESION_360", "Fuerza con que empuja la hoja (° de señal desde el quieto)"),
]
NOMBRES_CLASES = {
    "pagina_nueva": "Página nueva",
    "misma_pagina": "Misma página",
    "dos_hojas": "Dos hojas",
    "hoja_doblada": "Hoja doblada",
}


def crear_app(orquestador, db, enlace, cfg: dict, escucha=None) -> FastAPI:
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

    def _info_camara() -> dict:
        c = orquestador.camara
        res = getattr(c, "resolucion", None)
        return {"fuente": getattr(c, "fuente", None), "resolucion": list(res) if res else None,
                "conmutable": hasattr(c, "cambiar")}

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
        e["camara"] = _info_camara()
        e["claude"] = _info_claude()
        e["microfono"] = escucha.descripcion_microfono if escucha else None
        return JSONResponse(e)

    @app.post("/control/iniciar")
    def control_iniciar(titulo: str | None = Form(None), libro: str | None = Form(None),
                        modo: str | None = Form(None), max_paginas: int | None = Form(None)):
        # Sin formulario es el botón «Iniciar lectura / seguir»: lo mismo que la tecla A
        # (sigue si hay una lectura en pausa; si no hay documento, empieza una lectura).
        if modo is None:
            orquestador.orden("iniciar")
            return {"ok": True}
        try:
            orquestador.iniciar_documento((titulo or "").strip() or "Documento", (libro or "").strip() or "libro", modo,
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
        camara = orquestador.camara
        opciones = camara.opciones() if hasattr(camara, "opciones") else []
        return pagina("calibracion.html", request, parametros=PARAMETROS_CFG, msg=msg,
                      ocupado=orquestador.ocupado, hay_enlace=enlace is not None,
                      camara=_info_camara(), opciones_camara=opciones, claude=_info_claude())

    def _info_claude() -> dict | None:
        from ..vision.ocr_claude import MODELOS, nombre_visible

        o = orquestador.ocr
        if not hasattr(o, "hay_clave"):
            return None  # ocr.claude.activo: false
        return {"modelo": o.modelo, "modelos": MODELOS, "hay_clave": o.hay_clave, "ultimo": o.ultimo,
                "nombre": nombre_visible(o.modelo), "nombre_ultimo": nombre_visible(o.ultimo),
                "nombres": {m: nombre_visible(m) for m in MODELOS}}

    @app.post("/ocr/modelo")
    def ocr_modelo(modelo: str = Form(...)):
        from ..vision.ocr_claude import MODELOS

        if _info_claude() is None or modelo not in MODELOS:
            raise HTTPException(400, "modelo desconocido o lectura con IA desactivada")
        orquestador.ocr.modelo = modelo
        msg = "Lectura con " + _info_claude()["nombre"]
        return RedirectResponse(f"/calibracion?msg={quote(msg)}", status_code=303)

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

    # --- control manual de cada componente -----------------------------------------
    @app.get("/manual", response_class=HTMLResponse)
    def manual(request: Request):
        return pagina("manual.html", request, hay_enlace=enlace is not None, ocupado=orquestador.ocupado)

    @app.post("/manual/comando")
    def manual_comando(comando: str = Form(...), pieza: str = Form(""), valor: str = Form(""),
                       ms: str = Form(""), quieto: str = Form("")):
        """Una orden del panel de control manual; responde JSON para no recargar la página."""
        permitidos = {"PING", "STATUS", "HOME", "STOP", "BEEP", "MOVE", "FREE", "SPIN", "POS", "CFG"}
        if comando not in permitidos:
            raise HTTPException(400, "comando no permitido")
        if enlace is None:
            raise HTTPException(409, "no hay enlace con el ESP32")
        lectura = comando in ("POS", "CFG") and not valor.strip()
        if orquestador.ocupado and comando not in ("STATUS", "STOP", "PING", "FREE") and not lectura:
            raise HTTPException(409, "hay un documento en curso: termínelo antes de mover piezas a mano")
        args: list[object] = []
        if comando in ("MOVE", "FREE"):
            if pieza not in P.PIEZAS_MOVE or (comando == "FREE" and pieza == "M"):
                raise HTTPException(400, "pieza desconocida")
            args.append(pieza)
        if comando == "CFG":  # leer (sin valor) o guardar un parámetro de calibración
            if pieza not in dict(PARAMETROS_CFG):
                raise HTTPException(400, "parámetro desconocido")
            args.append(pieza)
        if comando in ("CFG", "POS") and valor.strip():
            try:
                args.append(int(valor))
            except ValueError:
                raise HTTPException(400, "valor no numérico") from None
        if comando == "FREE" and valor.strip():  # FREE,B,<señal de quieto>
            try:
                args.append(int(valor))
            except ValueError:
                raise HTTPException(400, "señal no numérica") from None
        if comando in ("MOVE", "BEEP", "SPIN"):
            try:
                args.append(int(valor))
                if comando == "SPIN":  # señal, duración y señal de quieto del servo de 360°
                    args += [int(ms), int(quieto)]
            except ValueError:
                raise HTTPException(400, "falta un valor numérico") from None
        texto = " ".join(map(str, [comando, *args]))
        try:
            r = enlace.comando(comando, *args)
        except RechazoESP32 as exc:
            ayuda = {"E02": "el ESP32 no conoce este comando: grabe el firmware 2.3.0",
                     "E03": "el ESP32 todavía se está moviendo",
                     "E05": "valor fuera de rango",
                     "E06": "el ESP32 está en PARADA: use «Volver a reposo»"}.get(exc.codigo, "")
            return JSONResponse({"ok": False, "orden": texto, "respuesta": f"NAK {exc.codigo}", "ayuda": ayuda})
        except ErrorEnlace as exc:
            return JSONResponse({"ok": False, "orden": texto, "respuesta": str(exc), "ayuda": ""})
        return JSONResponse({"ok": r.ok, "orden": texto, "respuesta": " ".join([r.cmd, ",".join(r.args)]).strip(),
                             "ms": round(r.latencia * 1000), "firmware": getattr(enlace, "version_firmware", None),
                             "ayuda": P.CODIGOS_ERROR.get(r.codigo, "") if r.codigo else ""})

    # --- cámara --------------------------------------------------------------------
    @app.post("/camara/fuente")
    def camara_fuente(fuente: str = Form(...)):
        camara = orquestador.camara
        if not hasattr(camara, "cambiar"):
            raise HTTPException(409, "esta cámara no se puede cambiar en caliente")
        if orquestador.ocupado:
            # Las fotos «antes» y «después» de un volteo deben ser de la misma cámara
            raise HTTPException(409, "hay un documento en curso")
        try:
            cambio = camara.cambiar(fuente.strip())
            msg = f"Cámara activa: {camara.fuente}" if cambio else f"{camara.fuente} ya era la cámara activa"
        except Exception as exc:  # la cámara nueva no abrió: sigue la anterior
            log.warning("No se pudo cambiar a la cámara %s: %s", fuente, exc)
            msg = f"No se pudo abrir {fuente} ({exc}); sigue {camara.fuente}"
        return RedirectResponse(f"/calibracion?msg={quote(msg)}", status_code=303)

    @app.get("/camara/foto.jpg")
    def camara_foto():
        if orquestador.ocupado:
            raise HTTPException(409, "hay un documento en curso")
        try:
            img = orquestador.camara.capturar()
        except Exception as exc:
            raise HTTPException(503, f"la cámara no entregó imagen: {exc}") from exc
        if img.shape[1] > 1280:
            esc = 1280 / img.shape[1]
            img = cv2.resize(img, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise HTTPException(500, "no se pudo codificar la foto")
        return Response(jpg.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return app
