"""Base de datos SQLite de documentos, páginas, volteos y pares (sección 1.4:
"registrar cada documento en una base de datos consultable desde una página web")."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    libro TEXT NOT NULL,
    modo TEXT NOT NULL,
    creado TEXT NOT NULL,
    terminado TEXT,
    paginas INTEGER NOT NULL DEFAULT 0,
    ruta_pdf TEXT
);
CREATE TABLE IF NOT EXISTS paginas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER NOT NULL REFERENCES documentos(id),
    numero INTEGER NOT NULL,              -- orden de captura
    numero_impreso INTEGER,               -- leído por el OCR
    ruta_imagen TEXT NOT NULL,
    ruta_ocr TEXT,
    categoria TEXT,
    confianza_categoria REAL,
    categoria_manual TEXT,                -- etiqueta puesta desde el panel
    calidad REAL,                         -- nitidez (varianza del Laplaciano)
    texto TEXT,
    creada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS volteos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER REFERENCES documentos(id),
    intento INTEGER NOT NULL,
    comando TEXT NOT NULL,
    respuesta TEXT,
    duracion_ms INTEGER,
    clase TEXT,
    confianza REAL,
    fuente TEXT,
    regla_pixeles TEXT,
    accion TEXT,
    ruta_par TEXT,
    creado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruta TEXT NOT NULL UNIQUE,
    libro TEXT NOT NULL,
    prediccion TEXT,
    regla_pixeles TEXT,
    etiqueta TEXT,
    creado TEXT NOT NULL
);
"""


def _ahora() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class BaseDatos:
    def __init__(self, ruta: str | Path):
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(ruta), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.con.executescript(ESQUEMA)
            try:
                self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS texto_fts USING fts5(texto, pagina_id UNINDEXED)")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            self.con.commit()

    def _ejecutar(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.con.execute(sql, params)
            self.con.commit()
            return cur

    def _consultar(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    # --- documentos -----------------------------------------------------------
    def crear_documento(self, titulo: str, libro: str, modo: str) -> int:
        return self._ejecutar(
            "INSERT INTO documentos (titulo, libro, modo, creado) VALUES (?, ?, ?, ?)",
            (titulo, libro, modo, _ahora()),
        ).lastrowid

    def terminar_documento(self, doc_id: int, ruta_pdf: str | None) -> None:
        self._ejecutar("UPDATE documentos SET terminado = ?, ruta_pdf = ? WHERE id = ?", (_ahora(), ruta_pdf, doc_id))

    def fijar_pdf(self, doc_id: int, ruta_pdf: str) -> None:
        self._ejecutar("UPDATE documentos SET ruta_pdf = ? WHERE id = ?", (ruta_pdf, doc_id))

    def documento(self, doc_id: int) -> dict | None:
        filas = self._consultar("SELECT * FROM documentos WHERE id = ?", (doc_id,))
        return filas[0] if filas else None

    def documentos(self) -> list[dict]:
        return self._consultar("SELECT * FROM documentos ORDER BY id DESC")

    # --- páginas -------------------------------------------------------------
    def agregar_pagina(self, doc_id: int, numero: int, ruta_imagen: str, ruta_ocr: str | None,
                       texto: str, numero_impreso: int | None, categoria: str, confianza: float,
                       calidad: float) -> int:
        pid = self._ejecutar(
            """INSERT INTO paginas (documento_id, numero, numero_impreso, ruta_imagen, ruta_ocr, categoria,
               confianza_categoria, calidad, texto, creada) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, numero, numero_impreso, ruta_imagen, ruta_ocr, categoria, confianza, calidad, texto, _ahora()),
        ).lastrowid
        self._ejecutar("UPDATE documentos SET paginas = paginas + 1 WHERE id = ?", (doc_id,))
        if self.fts and texto:
            self._ejecutar("INSERT INTO texto_fts (texto, pagina_id) VALUES (?, ?)", (texto, pid))
        return pid

    def paginas(self, doc_id: int) -> list[dict]:
        return self._consultar("SELECT * FROM paginas WHERE documento_id = ? ORDER BY numero", (doc_id,))

    def etiquetar_pagina(self, pagina_id: int, categoria: str) -> None:
        self._ejecutar("UPDATE paginas SET categoria_manual = ? WHERE id = ?", (categoria, pagina_id))

    def paginas_etiquetadas(self) -> list[dict]:
        return self._consultar(
            """SELECT p.*, d.libro FROM paginas p JOIN documentos d ON d.id = p.documento_id
               WHERE p.categoria_manual IS NOT NULL"""
        )

    def buscar(self, consulta: str, limite: int = 50) -> list[dict]:
        if self.fts:
            try:
                return self._consultar(
                    """SELECT p.id, p.documento_id, p.numero, d.titulo,
                              snippet(texto_fts, 0, '[', ']', '…', 12) AS fragmento
                       FROM texto_fts JOIN paginas p ON p.id = texto_fts.pagina_id
                       JOIN documentos d ON d.id = p.documento_id
                       WHERE texto_fts MATCH ? LIMIT ?""",
                    (consulta, limite),
                )
            except sqlite3.OperationalError:
                pass  # sintaxis FTS inválida: se busca con LIKE
        return self._consultar(
            """SELECT p.id, p.documento_id, p.numero, d.titulo, substr(p.texto, 1, 120) AS fragmento
               FROM paginas p JOIN documentos d ON d.id = p.documento_id
               WHERE p.texto LIKE ? LIMIT ?""",
            (f"%{consulta}%", limite),
        )

    # --- volteos y pares -----------------------------------------------------
    def registrar_volteo(self, **campos) -> int:
        campos.setdefault("creado", _ahora())
        columnas = ", ".join(campos)
        marcas = ", ".join("?" for _ in campos)
        return self._ejecutar(f"INSERT INTO volteos ({columnas}) VALUES ({marcas})", tuple(campos.values())).lastrowid

    def volteos(self, doc_id: int | None = None, limite: int = 200) -> list[dict]:
        if doc_id is None:
            return self._consultar("SELECT * FROM volteos ORDER BY id DESC LIMIT ?", (limite,))
        return self._consultar("SELECT * FROM volteos WHERE documento_id = ? ORDER BY id", (doc_id,))

    def registrar_par(self, ruta: str, libro: str, prediccion: str, regla: str) -> int:
        return self._ejecutar(
            "INSERT OR IGNORE INTO pares (ruta, libro, prediccion, regla_pixeles, creado) VALUES (?, ?, ?, ?, ?)",
            (ruta, libro, prediccion, regla, _ahora()),
        ).lastrowid

    def par_sin_etiqueta(self) -> dict | None:
        filas = self._consultar("SELECT * FROM pares WHERE etiqueta IS NULL ORDER BY id LIMIT 1")
        return filas[0] if filas else None

    def par(self, par_id: int) -> dict | None:
        filas = self._consultar("SELECT * FROM pares WHERE id = ?", (par_id,))
        return filas[0] if filas else None

    def etiquetar_par(self, par_id: int, etiqueta: str | None) -> None:
        self._ejecutar("UPDATE pares SET etiqueta = ? WHERE id = ?", (etiqueta, par_id))

    def pares_etiquetados(self) -> list[dict]:
        return self._consultar("SELECT * FROM pares WHERE etiqueta IS NOT NULL ORDER BY id")

    def conteo_pares(self) -> dict[str, int]:
        filas = self._consultar("SELECT COALESCE(etiqueta, 'sin_etiqueta') AS e, COUNT(*) AS n FROM pares GROUP BY e")
        return {f["e"]: f["n"] for f in filas}

    def estadisticas_volteo(self) -> dict:
        """Tasa de volteos correctos y de hojas dobles (criterio de 2.4.5 y T3.4)."""
        filas = self._consultar("SELECT clase, COUNT(*) AS n FROM volteos WHERE clase IS NOT NULL GROUP BY clase")
        conteo = {f["clase"]: f["n"] for f in filas}
        total = sum(conteo.values())
        return {
            "total": total,
            "por_clase": conteo,
            "tasa_correctos": conteo.get("pagina_nueva", 0) / total if total else None,
            "tasa_dobles": conteo.get("dos_hojas", 0) / total if total else None,
        }
