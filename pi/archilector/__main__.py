"""Línea de comandos de ArchiLector.

    python -m archilector ejecutar                     # robot completo + panel web
    python -m archilector ejecutar --puerto sim:// --camara sim --sin-voz   # todo simulado en un PC
    python -m archilector prueba-enlace                # pruebas T2.1 y TC.1–TC.6 con el ESP32 real
    python -m archilector consola                      # enviar comandos a mano (calibración)
    python -m archilector pdf 3                        # regenerar el PDF del documento 3
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__
from . import config as configuracion

log = logging.getLogger("archilector")


def _abrir_enlace(cfg: dict, puerto: str, libro, al_evento):
    from .enlace.esp32 import EnlaceESP32, abrir_puerto

    if puerto == "ninguno":
        return None
    sim = cfg["simulacion"]
    serie = abrir_puerto(puerto, libro=libro, escala_tiempo=sim["escala_tiempo"])
    reabrir = None if puerto.startswith("sim://") else (lambda: abrir_puerto(puerto))  # USB desenchufado y vuelto a enchufar
    return EnlaceESP32(serie, latido=cfg["enlace"]["latido"], al_evento=al_evento, reabrir=reabrir)


def construir(cfg: dict, puerto: str, fuente_camara: str, sin_voz: bool):
    """Arma todos los bloques. Devuelve (orquestador, enlace, db, escucha)."""
    from . import camara as cam
    from .ia.clasificador import Clasificador
    from .ia.registro_pares import RegistroPares
    from .ia.verificador import Verificador
    from .orquestador import Orquestador
    from .salida.db import BaseDatos
    from .simulacion import LibroSimulado
    from .vision.ocr_tesseract import crear as crear_ocr
    from .voz import stt, tts

    datos = Path(cfg["datos"])
    datos.mkdir(parents=True, exist_ok=True)
    libro = None
    if puerto.startswith("sim://") or fuente_camara == "sim":
        sim = cfg["simulacion"]
        libro = LibroSimulado(hojas=sim["hojas"], prob_fallas=sim["prob_fallas"])

    ia = cfg["ia"]
    verificador = Verificador(ia["modelo_verificador"], ia["umbral_regla_pixeles"], cfg["vision"]["region_canto"])

    def al_cambiar_camara(fuente: str) -> None:
        # Cada cámara queda encuadrada distinto: el recorte del canto es propio de cada una
        verificador.region_canto = tuple(cam.region_canto(cfg, fuente))

    camara = cam.crear(fuente_camara, libro, cfg["camara"], datos, al_cambiar_camara)
    clasificador = Clasificador(ia["modelo_clasificador"])
    ocr = crear_ocr(cfg["ocr"])
    db = BaseDatos(datos / "archilector.db")
    voz = tts.VozConsola() if sin_voz else tts.crear(cfg["voz"])
    registro = RegistroPares(datos / "pares", ia["guardar_pares"])

    orq = Orquestador(cfg, None, camara, ocr, verificador, clasificador, db, voz, registro)
    enlace = _abrir_enlace(cfg, puerto, libro, orq.al_evento)
    orq.enlace = enlace
    escucha = None if sin_voz else stt.crear(cfg["voz"], orq.orden_de_voz, lambda: voz.hablando)
    return orq, enlace, db, escucha


# =============================================================================
def cmd_ejecutar(args, cfg) -> int:
    import uvicorn

    from .enlace.esp32 import ErrorEnlace
    from .web.app import crear_app

    puerto = args.puerto or cfg["enlace"]["puerto"]
    fuente = args.camara or cfg["camara"]["fuente"]
    orq, enlace, db, escucha = construir(cfg, puerto, fuente, args.sin_voz)
    if enlace is not None:
        try:
            enlace.conectar()
            orq._lcd("LISTO", "A=LEER #=ARCHIVO")
            orq._luz(cfg["camara"]["luz_reposo"])
        except ErrorEnlace as exc:
            log.error("No se pudo hablar con el ESP32 (%s). Revise el cable C16 y la regla udev.", exc)
    if escucha:
        escucha.iniciar()
    log.info("Verificador: %s", "modelo entrenado" if orq.verificador.modelo else "regla de píxeles (sin modelo)")
    threading.Thread(target=orq._decir, args=("ArchiLector listo. Pulse A para leer o numeral para archivar.",),
                     daemon=True).start()
    app = crear_app(orq, db, enlace, cfg, escucha=escucha)
    host, port = cfg["web"]["host"], args.web_puerto or cfg["web"]["puerto"]
    log.info("Panel web en http://%s:%s", "localhost" if host == "0.0.0.0" else host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


# =============================================================================
def cmd_prueba_enlace(args, cfg) -> int:
    """Pruebas T2.1 y TC.1–TC.6 de la sección 8.9, con el ESP32 conectado."""
    from .enlace import protocolo as P
    from .enlace.esp32 import EnlaceESP32, ErrorEnlace, RechazoESP32, abrir_puerto

    puerto = args.puerto or cfg["enlace"]["puerto"]
    if not args.si and not puerto.startswith("sim://"):
        print("ATENCIÓN: TC.2, TC.3 y TC.5 mueven el mecanismo. Retire el libro o use uno de prueba.")
        if input("¿Continuar? [s/N] ").strip().lower() != "s":
            return 1
    serie = abrir_puerto(puerto, escala_tiempo=cfg["simulacion"]["escala_tiempo"])
    enlace = EnlaceESP32(serie, latido=True)
    resultados: list[tuple[str, bool, str]] = []

    def anotar(nombre, ok, detalle):
        resultados.append((nombre, ok, detalle))
        print(f"{nombre:<20} {'Pasa' if ok else 'FALLA':<6} {detalle}")

    try:
        r = enlace.comando("PING")
        if len(r.args) >= 2:
            enlace.version_firmware, enlace.version_protocolo = r.args[0], r.args[1]
        anotar("T2.1 Enlace", r.cmd == "PONG" and r.latencia < 0.05,
               f"{r.cmd},{','.join(r.args)} en {r.latencia * 1000:.1f} ms (≤ 50 ms)")
        estado = enlace.comando("STATUS")
        if estado.args and estado.args[0] != "REPOSO":
            enlace.comando("HOME")

        # SEQ lejos de los que usa el enlace, para no chocar con la caché de duplicados
        seq = (enlace._seq + 120) % 255 + 1
        malo = f"${seq},TURN*00\n"
        resp = enlace.trama_cruda(seq, malo, 0.5)
        anotar("TC.1 Formato", resp == ["NAK,E01"], f"{malo.strip()} → {resp}")

        seq = (seq + 7) % 255 + 1
        bueno = P.armar(seq, "TURN")
        resp = enlace.trama_cruda(seq, bueno, 14.0, repeticiones=2)
        dones = [x for x in resp if x.startswith(("DONE", "FAIL"))]
        anotar("TC.2 Duplicado", resp[:2] == ["ACK", "ACK"] and len(dones) == 1,
               f"{bueno.strip()} ×2 → {resp} (confirme a la vista: un solo volteo)")

        hilo = threading.Thread(target=enlace.comando, args=("TURN",))
        hilo.start()
        time.sleep(0.3)
        try:
            enlace._comando("TURN", (), None)
            anotar("TC.3 Ocupado", False, "el segundo TURN no fue rechazado")
        except RechazoESP32 as e:
            anotar("TC.3 Ocupado", e.codigo == "E03", f"NAK,{e.codigo}")
        hilo.join()

        anotar("TC.4 Corte de enlace", True, "MANUAL: desconecte el USB en reposo; en 5 s la LCD dice SIN ENLACE")

        resultado = {}
        hilo = threading.Thread(target=lambda: resultado.setdefault("r", enlace.comando("TURN")))
        hilo.start()
        time.sleep(1.0)
        r = enlace.comando("STOP")
        hilo.join()
        anotar("TC.5 Parada", r.args == ["PARADA"] and r.latencia <= 0.1,
               f"STATE,{','.join(r.args)} en {r.latencia * 1000:.0f} ms; TURN → {resultado['r'].cmd},"
               f"{','.join(resultado['r'].args)}")
        enlace.comando("HOME")

        fallas = 0
        t0 = time.monotonic()
        for _ in range(1000):
            try:
                if enlace.comando("PING").cmd != "PONG":
                    fallas += 1
            except ErrorEnlace:
                fallas += 1
        anotar("TC.6 Resistencia", fallas == 0, f"1000 PING en {time.monotonic() - t0:.1f} s, {fallas} fallas")
    except ErrorEnlace as exc:
        anotar("Enlace", False, str(exc))
    finally:
        enlace.cerrar()
    print(f"\nFirmware {enlace.version_firmware or '?'} · anote cada fila en la hoja de registro (sección 10.2).")
    return 0 if all(ok for _, ok, _ in resultados) else 2


# =============================================================================
def cmd_consola(args, cfg) -> int:
    """Envía comandos escritos a mano: «TURN», «CFG PASOS_ARCO 1100», «LCD 1 HOLA»."""
    from .enlace.esp32 import EnlaceESP32, ErrorEnlace, abrir_puerto

    puerto = args.puerto or cfg["enlace"]["puerto"]
    enlace = EnlaceESP32(abrir_puerto(puerto, escala_tiempo=cfg["simulacion"]["escala_tiempo"]), latido=True,
                         al_evento=lambda ev: print(f"\n  << EVT {ev.tipo} {ev.valor} {' '.join(ev.extra)}"))
    print("Consola ArchiLector. Ejemplos: PING · STATUS · HOME · TURN · RETRY 40 · CFG PASOS_ARCO 1100 · salir")
    try:
        while True:
            try:
                linea = input("> ").strip()
            except EOFError:
                break
            if not linea:
                continue
            if linea.lower() in ("salir", "exit", "q"):
                break
            partes = linea.split(maxsplit=2) if linea.upper().startswith("LCD") else linea.split()
            cmd, argumentos = partes[0].upper(), partes[1:]
            try:
                r = enlace.comando(cmd, *argumentos)
                print(f"  {r.cmd} {','.join(r.args)}  ({r.latencia * 1000:.0f} ms)")
            except (ErrorEnlace, ValueError) as exc:
                print(f"  ! {exc}")
    finally:
        enlace.cerrar()
    return 0


def cmd_pdf(args, cfg) -> int:
    orq, enlace, db, _ = construir(cfg, "ninguno", "sim", True)
    print(orq.generar_pdf(args.documento))
    return 0


# =============================================================================
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="archilector", description=f"ArchiLector {__version__}")
    p.add_argument("--config", default="config.yaml", help="archivo de configuración (por defecto config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="comando", required=True)

    e = sub.add_parser("ejecutar", help="robot completo con panel web")
    e.add_argument("--puerto", help="/dev/archilector-esp32, sim:// o ninguno (modo asistido sin ESP32)")
    e.add_argument("--camara", help="auto, pi, webcam[:<n>|:<ruta>], sim o carpeta:<ruta>")
    e.add_argument("--sin-voz", action="store_true", help="la voz se escribe en el registro")
    e.add_argument("--web-puerto", type=int)

    t = sub.add_parser("prueba-enlace", help="pruebas T2.1 y TC.1–TC.6")
    t.add_argument("--puerto")
    t.add_argument("--si", action="store_true", help="no preguntar antes de mover el mecanismo")

    c = sub.add_parser("consola", help="enviar comandos al ESP32 a mano")
    c.add_argument("--puerto")

    d = sub.add_parser("pdf", help="regenerar el PDF de un documento")
    d.add_argument("documento", type=int)

    args = p.parse_args(argv)
    for flujo in (sys.stdout, sys.stderr):  # consolas de Windows que no son UTF-8
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = configuracion.cargar(args.config)
    if Path(".env").exists():  # secretos (ANTHROPIC_API_KEY): fuera del repositorio, ver .gitignore
        for linea in Path(".env").read_text(encoding="utf-8").splitlines():
            clave, _, valor = linea.partition("=")
            if clave.strip() and not clave.startswith("#"):
                os.environ.setdefault(clave.strip(), valor.strip())
    return {"ejecutar": cmd_ejecutar, "prueba-enlace": cmd_prueba_enlace, "consola": cmd_consola,
            "pdf": cmd_pdf}[args.comando](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
