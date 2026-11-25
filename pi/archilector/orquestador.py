"""Ciclo de operación de ArchiLector (Diseño 2.5 y figura 8.2 del Técnico).

La Raspberry Pi decide y el ESP32 mueve:

1. La cámara fotografía la página bajo luz LED fija.
2. Se corrige la imagen, se lee el texto (OCR) y se clasifica la página.
3. Modo archivo: la página se guarda y el volteo se pide de inmediato.
   Modo lectura: Piper lee la página; el volteo se pide al terminar o cuando
   el usuario dice «siguiente».
   Modo asistido (plan B, Diseño 2.4.8): la persona pasa la hoja y el sistema
   detecta el cambio; todo lo demás es igual.
4. ``TURN`` → ``DONE`` → foto → verificador:
   * página nueva  → esa foto es la siguiente página
   * misma página  → ``RETRY,40``, hasta 2 reintentos
   * dos hojas     → ``BACK`` y se verifica de nuevo
   * hoja doblada  → LCD «REVISE LA HOJA», 3 pitidos, espera la tecla #
   * confianza < 0,80 → otra foto; si se repite, se trata como hoja doblada
5. Tras los reintentos fallidos el sistema se detiene, suena el buzzer, la LCD
   muestra el error y, en modo lectura, avisa por voz.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .enlace import protocolo as P
from .enlace.esp32 import EnlaceCaido, ErrorEnlace, Evento, RechazoESP32
from .ia import clasificador as C
from .ia import regla_pixeles
from .ia.verificador import DOS_HOJAS, HOJA_DOBLADA, MISMA_PAGINA, PAGINA_NUEVA, Veredicto
from .salida import pdf
from .vision import preproceso
from .vision.ocr import ResultadoOCR

log = logging.getLogger(__name__)

ARCHIVO, LECTURA, ASISTIDO = "archivo", "lectura", "asistido"
MODOS = (ARCHIVO, LECTURA, ASISTIDO)

# Tabla 7.2: tecla -> orden
TECLAS = {
    "A": "iniciar", "B": "pausa", "C": "repetir", "D": "parar", "*": "anterior", "#": "confirmar",
    "1": "volumen-", "2": "volumen+", "4": "lenta", "6": "rapida", "0": "ayuda",
}
# Órdenes que cortan la lectura en curso
INTERRUMPEN = {"pausa", "parar", "siguiente", "repetir", "anterior", "terminar"}

AYUDA = (
    "Tecla A: iniciar o continuar. B: pausa. C: repetir la página. D: parada inmediata. "
    "Asterisco: página anterior. Numeral: confirmar. 1 y 2: volumen. 4 y 6: velocidad de la voz. "
    "También puede decir: siguiente, repite, pausa, continuar o anterior."
)


class FinDocumento(Exception):
    """El usuario pidió terminar el documento."""


@dataclass
class Captura:
    foto: np.ndarray
    corregida: preproceso.FotoCorregida
    ocr: ResultadoOCR
    categoria: str = C.TEXTO

    @property
    def numero_impreso(self) -> int | None:
        return self.ocr.numero_pagina


@dataclass
class EstadoPublico:
    estado: str = "inactivo"
    modo: str | None = None
    documento_id: int | None = None
    titulo: str | None = None
    paginas: int = 0
    intento: int = 0
    mensaje: str = ""
    ultimo_veredicto: dict = field(default_factory=dict)
    ultima_pagina: str | None = None


class Orquestador:
    def __init__(self, cfg: dict, enlace, camara, ocr, verificador, clasificador, db, voz, registro):
        self.cfg = cfg
        self.enlace = enlace
        self.camara = camara
        self.ocr = ocr
        self.verificador = verificador
        self.clasificador = clasificador
        self.db = db
        self.voz = voz
        self.registro = registro
        self.datos = Path(cfg["datos"])
        self.ordenes: queue.Queue[str] = queue.Queue()
        self.cortar = threading.Event()   # interrumpe la lectura en voz alta
        self.publico = EstadoPublico()
        self._hilo: threading.Thread | None = None
        self._lock = threading.Lock()
        self._texto_anterior = ""
        self._libro = ""
        self._numero = 0
        self._esp_en_parada = False
        self._max_paginas: int | None = None
        # Último número impreso leído: (orden de captura, número impreso)
        self._ref_impreso: tuple[int, int] | None = None
        # Lectura adelantada: mientras se lee una página, otro hilo pasa la hoja y lee la siguiente.
        # Ese hilo espera a que la voz quede libre antes de hablar o de atender una tecla.
        self._voz_libre = threading.Event()
        self._voz_libre.set()
        self._adelanto: tuple[threading.Thread, dict] | None = None
        self._pausas = 0
        self._pausas_al_adelantar = 0

    # =========================================================================
    # Entradas: teclado del ESP32, voz y panel web
    # =========================================================================
    def orden(self, nombre: str) -> None:
        log.info("Orden: %s", nombre)
        if nombre in INTERRUMPEN:
            self.cortar.set()
        # Sin documento en curso: A inicia la lectura y # el archivo (elección del modo, Diseño 2.5)
        if not self.ocupado:
            if nombre == "iniciar":
                self.iniciar_documento(f"Lectura {time.strftime('%Y-%m-%d %H:%M')}", "libro", LECTURA)
                return
            if nombre == "confirmar":
                self.iniciar_documento(f"Archivo {time.strftime('%Y-%m-%d %H:%M')}", "libro", ARCHIVO)
                return
            if nombre == "ayuda":
                threading.Thread(target=self._decir, args=(AYUDA,), daemon=True).start()
                return
        self.ordenes.put(nombre)

    def al_evento(self, ev: Evento) -> None:
        """Callback de ``EnlaceESP32`` para los eventos ``EVT`` (SEQ 0)."""
        if ev.tipo == "KEY" and ev.valor in TECLAS:
            if ev.valor == "D":
                self._esp_en_parada = True  # el ESP32 ya cortó los actuadores
            self.orden(TECLAS[ev.valor])
        elif ev.tipo == "STATE":
            if ev.valor == "PARADA":
                self._esp_en_parada = True
                self.cortar.set()
            if "E07" in ev.extra:
                log.error("E07: el ESP32 se reinició (¿caída de tensión? revisar T1.4)")
                self._esp_en_parada = True  # se exige HOME antes de seguir
                self.publico.mensaje = "E07: el ESP32 se reinició; revise la alimentación"
                self.cortar.set()
                self.ordenes.put("parar")

    @property
    def ocupado(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    # =========================================================================
    # Documento
    # =========================================================================
    def iniciar_documento(self, titulo: str, libro: str, modo: str, max_paginas: int | None = None) -> int:
        if modo not in MODOS:
            raise ValueError(f"modo desconocido: {modo}")
        with self._lock:
            if self.ocupado:
                raise RuntimeError("ya hay un documento en curso")
            while not self.ordenes.empty():
                self.ordenes.get_nowait()
            self.cortar.clear()
            doc_id = self.db.crear_documento(titulo, libro, modo)
            self.publico = EstadoPublico(estado="iniciando", modo=modo, documento_id=doc_id, titulo=titulo)
            self._libro, self._numero, self._max_paginas = libro, 0, max_paginas
            self._texto_anterior = ""
            self._ref_impreso = None
            self._hilo = threading.Thread(target=self._ejecutar, args=(doc_id, modo), daemon=True, name="ciclo")
            self._hilo.start()
            return doc_id

    def esperar_fin(self, timeout: float | None = None) -> None:
        if self._hilo:
            self._hilo.join(timeout)

    def _ejecutar(self, doc_id: int, modo: str) -> None:
        try:
            self._preparar_esp32(modo)
            self._decir({LECTURA: "Modo lectura.", ARCHIVO: "Modo archivo.", ASISTIDO: "Modo asistido."}[modo])
            actual = self._analizar(self._capturar())
            while True:
                self._procesar_pagina(doc_id, modo, actual)
                ultima = bool(self._max_paginas and self._numero >= self._max_paginas)
                if modo == LECTURA:
                    if not ultima and self.enlace is not None and self.cfg["lectura"]["adelantar"]:
                        self._adelantar(actual)
                    self._leer(actual)  # también la última: el límite solo evita voltear otra hoja
                if ultima:
                    break
                if self._adelanto:
                    actual = self._recoger_adelanto()
                    self._revisar_ordenes_pendientes()
                    continue
                self._revisar_ordenes_pendientes()
                actual = self._esperar_cambio(actual) if modo == ASISTIDO else self._pasar_pagina(actual)
        except FinDocumento:
            log.info("Documento %s terminado por el usuario", doc_id)
        except EnlaceCaido as exc:
            log.error("Se perdió el enlace con el ESP32: %s", exc)
            self.publico.mensaje = "Se perdió el enlace con el ESP32"
            self._decir("Se perdió la conexión con el controlador.")
        except Exception:
            log.exception("Error en el ciclo")
            self.publico.mensaje = "Error interno; ver el registro"
        finally:
            if self._adelanto:  # terminó a mitad de una lectura: el volteo en curso también termina
                self.ordenes.put("terminar")
                try:
                    self._recoger_adelanto()
                except Exception:
                    pass
            self._finalizar(doc_id)

    def _adelantar(self, actual: Captura) -> None:
        resultado: dict = {}

        def trabajo():
            try:
                resultado["captura"] = self._pasar_pagina(actual)
            except BaseException as exc:  # FinDocumento, EnlaceCaido...: se relanzan en el hilo principal
                resultado["error"] = exc

        self._voz_libre.clear()
        self._pausas_al_adelantar = self._pausas
        hilo = threading.Thread(target=trabajo, daemon=True, name="adelanto")
        self._adelanto = (hilo, resultado)
        hilo.start()

    def _recoger_adelanto(self) -> Captura:
        hilo, resultado = self._adelanto
        self._voz_libre.set()  # ya puede hablar o esperar teclas si lo necesita
        hilo.join()
        self._adelanto = None
        if "error" in resultado:
            raise resultado["error"]
        return resultado["captura"]

    def _en_adelanto(self) -> bool:
        return threading.current_thread().name == "adelanto"

    def _preparar_esp32(self, modo: str) -> None:
        if modo == ASISTIDO or self.enlace is None:
            self._luz(self.cfg["camara"]["luz_captura"])
            return
        estado = self.enlace.comando("STATUS")
        if estado.args and estado.args[0] in ("PARADA", "FALLA"):
            self.enlace.comando("HOME")
        self._esp_en_parada = False
        self._luz(self.cfg["camara"]["luz_captura"])

    def _finalizar(self, doc_id: int) -> None:
        ruta = None
        try:
            ruta = str(self.generar_pdf(doc_id))
        except Exception:
            log.exception("No se pudo generar el PDF")
        self.db.terminar_documento(doc_id, ruta)
        self._luz(self.cfg["camara"]["luz_reposo"])
        self._lcd("LISTO", "A=LEER #=ARCHIVO")
        self.publico.estado = "terminado"
        self._decir(f"Documento terminado con {self._numero} páginas.")

    def generar_pdf(self, doc_id: int) -> Path:
        doc = self.db.documento(doc_id)
        paginas = [(p["ruta_imagen"], p["ruta_ocr"]) for p in self.db.paginas(doc_id)]
        if not paginas:
            raise ValueError("el documento no tiene páginas")
        ruta = self.datos / "documentos" / str(doc_id) / f"documento_{doc_id}.pdf"
        pdf.generar(paginas, ruta, titulo=doc["titulo"] if doc else "ArchiLector")
        self.db.fijar_pdf(doc_id, str(ruta))
        return ruta

    # =========================================================================
    # Captura y análisis de una página
    # =========================================================================
    def _capturar(self) -> np.ndarray:
        return self.camara.capturar()

    def _analizar(self, foto: np.ndarray) -> Captura:
        corregida = preproceso.corregir(foto, self.cfg["vision"]["lado_pagina"])
        return Captura(foto, corregida, self.ocr.leer(corregida.pagina))

    def _procesar_pagina(self, doc_id: int, modo: str, c: Captura) -> None:
        self._numero += 1
        n = self._numero
        texto = c.ocr.texto
        cat = self.clasificador.clasificar(c.corregida.pagina, texto)
        carpeta = self.datos / "documentos" / str(doc_id)
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta_img = carpeta / f"pagina_{n:04d}.jpg"
        ruta_ocr = carpeta / f"pagina_{n:04d}.json"
        cv2.imwrite(str(ruta_img), c.corregida.pagina, [cv2.IMWRITE_JPEG_QUALITY, 90])
        ruta_ocr.write_text(json.dumps(c.ocr.a_dict(), ensure_ascii=False), encoding="utf-8")
        self.db.agregar_pagina(doc_id, n, str(ruta_img), str(ruta_ocr), texto, c.numero_impreso,
                               cat.categoria, cat.confianza, c.corregida.nitidez)
        c.categoria = cat.categoria
        self.publico.paginas = n
        self.publico.ultima_pagina = str(ruta_img)
        if c.numero_impreso is not None:
            self._ref_impreso = (n, c.numero_impreso)
        impreso = f" (impresa {c.numero_impreso})" if c.numero_impreso else ""
        log.info("Página %d%s: %s, %d caracteres, nitidez %.0f", n, impreso, cat.categoria, len(texto),
                 c.corregida.nitidez)
        if modo == ARCHIVO:
            self._lcd("ARCHIVANDO", f"PAG {n:03d}")
        elif modo == ASISTIDO:
            self._beep(1)

    # =========================================================================
    # Modo lectura
    # =========================================================================
    def _texto_para_leer(self, c: Captura) -> str | None:
        categoria = c.categoria
        texto = c.ocr.texto.strip()
        if categoria == C.BLANCO:
            return None if self.cfg["lectura"]["saltar_blancas"] else "Página en blanco."
        prefijo = "Esta página tiene una tabla o un formulario. " if categoria == C.FORMULARIO_TABLA else ""
        return prefijo + (texto or "No se reconoció texto en esta página.")

    def _leer(self, c: Captura) -> None:
        texto = self._texto_para_leer(c)
        self.publico.estado = "leyendo"
        self._lcd("LEYENDO", f"PAG {self._numero:03d}")
        while True:
            if texto is None:
                break  # página en blanco: se salta
            self.cortar.clear()
            completa = self.voz.hablar(texto, self.cortar)
            if completa:
                if self.cfg["lectura"]["avance_automatico"]:
                    break
                o = self._esperar_orden({"siguiente", "iniciar", "repetir", "anterior", "pausa", "parar"})
            else:
                o = self._sacar_orden_pendiente() or "pausa"
            if o in ("siguiente", "iniciar"):
                break
            if o == "repetir":
                continue
            if o == "anterior":
                self.voz.hablar(self._texto_anterior or "No hay página anterior.", None)
                continue
            if o in ("pausa", "parar"):
                self._pausa(o)
                continue  # al reanudar se vuelve a leer la página desde el inicio
            if o == "terminar":
                raise FinDocumento
        self._texto_anterior = texto or ""

    # =========================================================================
    # Cambio de página (figura 8.2)
    # =========================================================================
    def _pasar_pagina(self, antes: Captura) -> Captura:
        reintentos = self.cfg["volteo"]["reintentos"]
        extra = self.cfg["volteo"]["pasos_extra_retry"]
        cmd: tuple = ("TURN",)
        intento = 1
        tras_back = False
        while True:
            self._revisar_ordenes_pendientes()
            self.publico.estado = "volteando"
            self.publico.intento = intento
            self._lcd("VOLTEANDO", f"INTENTO {intento}/{reintentos + 1}")
            respuesta = self._mover(*cmd)
            if respuesta is None:
                # Hubo parada o falla y el usuario ya intervino: se mira cómo quedó el libro
                cmd, intento, tras_back = ("TURN",), 1, False
                revisada = self._revisar_tras_intervencion(antes)
                if revisada is not None:
                    return revisada
                continue

            v, foto, despues = self._veredicto(antes)
            accion = self._decidir(v, intento, reintentos, tras_back)
            self._registrar_volteo(cmd, respuesta, v, intento, accion, antes, foto)

            if accion == "seguir":
                return despues
            if accion == "reintentar":
                intento += 1
                cmd = ("RETRY", extra)
                continue
            if accion == "regresar":
                tras_back = True
                cmd = ("BACK",)
                continue
            # accion == "usuario": se detiene, avisa y espera la tecla #
            self._pedir_ayuda(v.clase)
            cmd, intento, tras_back = ("TURN",), 1, False
            revisada = self._revisar_tras_intervencion(antes)
            if revisada is not None:
                return revisada

    def _decidir(self, v: Veredicto, intento: int, reintentos: int, tras_back: bool) -> str:
        if v.clase == PAGINA_NUEVA:
            return "seguir"
        if v.clase == MISMA_PAGINA:
            return "reintentar" if intento <= reintentos else "usuario"
        if v.clase == DOS_HOJAS:
            return "usuario" if tras_back else "regresar"
        return "usuario"  # hoja doblada

    def _veredicto(self, antes: Captura) -> tuple[Veredicto, np.ndarray, Captura | None]:
        """Foto de verificación + IA, con la regla de confianza (sección 4.5) y la
        segunda comprobación por número de página (sección 4.2)."""
        umbral = self.cfg["ia"]["umbral_confianza"]
        foto = self._capturar()
        v = self.verificador.verificar(antes.foto, foto)
        if v.confianza < umbral:
            log.info("Confianza %.2f < %.2f: se toma otra foto", v.confianza, umbral)
            foto = self._capturar()
            v = self.verificador.verificar(antes.foto, foto)
            if v.confianza < umbral:
                v.clase = HOJA_DOBLADA
        despues = None
        if v.clase == PAGINA_NUEVA:
            despues = self._analizar(foto)
            esperado = self._numero_esperado()
            d = despues.numero_impreso
            if esperado is not None and d is not None:
                paso = self.cfg["vision"]["paso_numeracion"]
                if d == esperado + paso:
                    log.warning("Se esperaba la página %s y se leyó %s: pasaron dos hojas", esperado, d)
                    v.clase = DOS_HOJAS
                elif d == esperado - paso:
                    log.warning("Se leyó otra vez la página %s: la hoja no pasó", d)
                    v.clase = MISMA_PAGINA
        self.publico.ultimo_veredicto = {
            "clase": v.clase, "confianza": round(v.confianza, 3), "fuente": v.fuente,
            "regla": v.regla, "ms": round(v.milisegundos, 1),
        }
        log.info("Verificador: %s (%.2f, %s; regla: %s)", v.clase, v.confianza, v.fuente, v.regla)
        return v, foto, despues

    def _numero_esperado(self) -> int | None:
        """Número impreso que debería tener la página siguiente, a partir del
        último número leído (aunque las páginas intermedias no lo muestren)."""
        if self._ref_impreso is None:
            return None
        orden, impreso = self._ref_impreso
        return impreso + self.cfg["vision"]["paso_numeracion"] * (self._numero + 1 - orden)

    def _registrar_volteo(self, cmd, respuesta, v: Veredicto, intento, accion, antes: Captura,
                          foto_despues: np.ndarray) -> None:
        ruta_par = self.registro.guardar(
            self._libro, antes.foto, foto_despues, v.imagen_compuesta,
            {"comando": ",".join(map(str, cmd)), "intento": intento, "prediccion": v.clase,
             "confianza": v.confianza, "fuente": v.fuente, "regla_pixeles": v.regla,
             "fraccion_cambio": v.fraccion_cambio, "duracion_ms": respuesta.duracion_ms,
             "camara": getattr(self.camara, "fuente", None)},
        ) if self.registro else None
        if ruta_par:
            self.db.registrar_par(str(ruta_par), self._libro, v.clase, v.regla)
        self.db.registrar_volteo(
            documento_id=self.publico.documento_id, intento=intento, comando=",".join(map(str, cmd)),
            respuesta=f"{respuesta.cmd},{','.join(respuesta.args)}", duracion_ms=respuesta.duracion_ms,
            clase=v.clase, confianza=v.confianza, fuente=v.fuente, regla_pixeles=v.regla, accion=accion,
            ruta_par=str(ruta_par) if ruta_par else None,
        )

    def _mover(self, *cmd) -> object | None:
        """Envía una orden de movimiento. Devuelve la respuesta DONE, o None si
        hubo que detenerse (FAIL, PARADA o NAK) y el usuario ya decidió seguir."""
        try:
            r = self.enlace.comando(*cmd)
        except RechazoESP32 as e:
            if e.codigo == "E03":
                time.sleep(0.5)
                return self._mover(*cmd)
            if e.codigo == "E06":
                self._pausa("parar")
                return None
            if e.codigo == "E05" and cmd[0] == "BACK":
                log.warning("BACK deshabilitado (rodillos con embrague): se pide ayuda")
                self._pedir_ayuda(DOS_HOJAS)
                return None
            raise
        if r.ok:
            return r
        if r.codigo == "E04":
            log.error("FAIL,E04: la secuencia superó su tiempo máximo")
            self._alarma("ERROR E04", "#=CONTINUAR", "El mecanismo se trabó. Revise el brazo y pulse numeral.")
            self._home()
            return None
        # FAIL,E06: la secuencia se interrumpió por STOP, tecla D o pérdida de enlace
        self._pausa("parar")
        return None

    def _home(self) -> None:
        r = self.enlace.comando("HOME")
        if not r.ok:
            raise ErrorEnlace(f"HOME falló: {r.codigo}")
        self._esp_en_parada = False

    def _revisar_tras_intervencion(self, antes: Captura) -> Captura | None:
        """Tras una parada o una ayuda del usuario: ¿quedó ya la página nueva?"""
        foto = self._capturar()
        v = self.verificador.verificar(antes.foto, foto)
        if v.clase == PAGINA_NUEVA and v.confianza >= self.cfg["ia"]["umbral_confianza"]:
            log.info("Tras la intervención ya hay una página nueva")
            return self._analizar(foto)
        return None

    def _pedir_ayuda(self, clase: str) -> None:
        mensajes = {
            MISMA_PAGINA: ("NO PASA LA HOJA", "La hoja no pasó. Pásela a mano y pulse numeral."),
            DOS_HOJAS: ("PASARON 2 HOJAS", "Pasaron dos hojas. Sepárelas y pulse numeral."),
            HOJA_DOBLADA: ("REVISE LA HOJA", "La hoja quedó doblada. Revísela y pulse numeral."),
        }
        linea1, voz = mensajes.get(clase, ("REVISE LA HOJA", "Revise la hoja y pulse numeral."))
        self._alarma(linea1, "#=CONTINUAR", voz)

    # =========================================================================
    # Modo asistido: la persona pasa la hoja
    # =========================================================================
    def _esperar_cambio(self, antes: Captura) -> Captura:
        umbral = self.cfg["ia"]["umbral_regla_pixeles"]
        self.publico.estado = "esperando_hoja"
        self._lcd("PASE LA HOJA", f"PAG {self._numero:03d}")
        while True:
            self._revisar_ordenes_pendientes()
            foto = self._capturar()
            if regla_pixeles.fraccion_cambio(antes.foto, foto) < umbral:
                time.sleep(0.5)
                continue
            # Hubo cambio: se espera a que la imagen se quede quieta (la mano salió)
            time.sleep(0.7)
            estable = self._capturar()
            if regla_pixeles.fraccion_cambio(foto, estable) > umbral / 2:
                continue
            v = self.verificador.verificar(antes.foto, estable)
            self.publico.ultimo_veredicto = {"clase": v.clase, "confianza": round(v.confianza, 3),
                                             "fuente": v.fuente, "regla": v.regla}
            if v.clase == PAGINA_NUEVA:
                return self._analizar(estable)
            if v.clase == DOS_HOJAS:
                self._decir("Pasaron dos hojas. Regrese una.")
            elif v.clase == HOJA_DOBLADA:
                self._decir("La hoja quedó doblada.")

    # =========================================================================
    # Órdenes del usuario, pausa y alarmas
    # =========================================================================
    def _orden_comun(self, o: str) -> bool:
        """Atiende las órdenes que valen en cualquier momento. True si la consumió."""
        if o == "volumen-":
            self.voz.ajustar_volumen(-10)
        elif o == "volumen+":
            self.voz.ajustar_volumen(+10)
        elif o == "lenta":
            self.voz.ajustar_velocidad(+0.1)
        elif o == "rapida":
            self.voz.ajustar_velocidad(-0.1)
        elif o == "ayuda":
            self._decir(AYUDA)
        else:
            return False
        return True

    def _sacar_orden_pendiente(self) -> str | None:
        while True:
            try:
                o = self.ordenes.get_nowait()
            except queue.Empty:
                return None
            if not self._orden_comun(o):
                return o

    def _revisar_ordenes_pendientes(self) -> None:
        # Mientras se lee en voz alta, las órdenes son de la lectura (repetir, pausa...), no del volteo
        o = self._sacar_orden_pendiente() if self._voz_libre.is_set() else None
        if o in ("pausa", "parar"):
            self._pausa(o)
        elif o == "terminar":
            raise FinDocumento
        if self._esp_en_parada and self.publico.modo != ASISTIDO:
            self._pausa("parar")

    def _esperar_orden(self, validas: set[str], timeout: float | None = None) -> str | None:
        fin = time.monotonic() + timeout if timeout else None
        while True:
            resto = None if fin is None else max(0.0, fin - time.monotonic())
            try:
                o = self.ordenes.get(timeout=resto if resto is not None else 0.5)
            except queue.Empty:
                if fin is not None and time.monotonic() >= fin:
                    return None
                continue
            if o == "terminar":
                raise FinDocumento
            if self._orden_comun(o):
                continue
            if o in validas:
                return o

    def _pausa(self, motivo: str) -> None:
        """PAUSA (tecla B) o PARADA (tecla D / STOP): A sigue, # termina el documento."""
        if self._en_adelanto():
            self._voz_libre.wait()
            if self._pausas != self._pausas_al_adelantar:
                # La lectura ya atendió esta pausa (p. ej. tecla D a mitad del volteo): solo falta el HOME
                if self._esp_en_parada:
                    self._home()
                return
        self._pausas += 1
        self.publico.estado = "parada" if motivo == "parar" else "pausa"
        self._lcd("PARADA" if motivo == "parar" else "PAUSA", "A=SEGUIR #=FIN")
        if motivo == "parar" and self.enlace is not None and self.publico.modo != ASISTIDO:
            try:
                if not self._esp_en_parada:
                    self.enlace.comando("STOP")
                self._esp_en_parada = True
            except ErrorEnlace:
                pass
        self._decir("Pausa. Pulse A para seguir o numeral para terminar.")
        o = self._esperar_orden({"iniciar", "confirmar", "siguiente"})
        if o == "confirmar":
            raise FinDocumento
        if self._esp_en_parada and self.publico.modo != ASISTIDO:
            self._home()  # E06: en PARADA el ESP32 exige HOME (tabla 8.5)
        self.cortar.clear()
        self.publico.estado = "reanudando"

    def _alarma(self, linea1: str, linea2: str, mensaje_voz: str) -> None:
        """Se detiene, suena el buzzer, la LCD muestra el error y, en modo
        lectura, avisa por voz. Espera la tecla # (o A)."""
        self._voz_libre.wait()  # en lectura adelantada, después de terminar la página que suena
        self.publico.estado = "requiere_usuario"
        self.publico.mensaje = mensaje_voz
        self._beep(3)
        self._lcd(linea1, linea2)
        if self.publico.modo in (LECTURA, ASISTIDO):
            self._decir(mensaje_voz)
        self._esperar_orden({"confirmar", "iniciar"})
        if self._esp_en_parada:
            self._home()
        self.publico.mensaje = ""

    # =========================================================================
    # Salidas hacia el ESP32 y la voz (nunca detienen el ciclo si fallan)
    # =========================================================================
    def _lcd(self, linea1: str, linea2: str) -> None:
        if self.enlace is None:
            return
        try:
            self.enlace.comando("LCD", 1, P.texto_lcd(linea1) or " ")
            self.enlace.comando("LCD", 2, P.texto_lcd(linea2) or " ")
        except ErrorEnlace as exc:
            log.debug("LCD: %s", exc)

    def _beep(self, patron: int) -> None:
        if self.enlace is None:
            return
        try:
            self.enlace.comando("BEEP", patron)
        except ErrorEnlace as exc:
            log.debug("BEEP: %s", exc)

    def _luz(self, porcentaje: int) -> None:
        if self.enlace is None:
            return
        try:
            self.enlace.comando("LIGHT", int(porcentaje))
            time.sleep(self.cfg["camara"]["espera_luz"])
        except ErrorEnlace as exc:
            log.debug("LIGHT: %s", exc)

    def _decir(self, texto: str) -> None:
        try:
            self.voz.hablar(texto, None)
        except Exception:  # pragma: no cover
            log.exception("Error de voz")
