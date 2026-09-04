"""
Copia de seguridad de la base de datos, enviable por Telegram (/backup).

Funciona en los dos motores:
  - SQLite  → copia CONSISTENTE del archivo con la API de backup de
    sqlite3 (segura aunque el bot esté escribiendo), lista para abrir
    con cualquier visor de SQLite.
  - Postgres → volcado JSON de todas las tablas (portable y re-importable).

El historial acumulado (billeteras, apariciones, señales, clusters,
predicciones) es lo que da valor al sistema: conviene respaldarlo.
"""

import gzip
import json
import os
import sqlite3
import tempfile
import time

import db as _db
from config import DB_PATH
from avisos import aviso as _avisar_ex   # (19-AE)

# (Ola 17-C, auditoria 4) Faltaban SIETE tablas, y el restaurador
# (restaurar_backup.ORDEN) SI las esperaba: el que respalda no escribia
# lo que el que restaura pide. Entre ellas `trades` — que migrate_to_pg
# describe literalmente como "lo mas valioso" —, `paper_fills` (el libro
# inmutable de salidas del paper: sin el se pierden los cierres
# parciales) y `wallet_identity`/`wallet_funding`, que son investigacion
# pagada con creditos de Helius.
# Solo afecta al volcado de Postgres: en SQLite se copia el archivo
# entero y siempre estuvieron dentro.
TABLES = ["wallets", "winning_tokens", "appearances", "signals",
          "settings", "predictions", "positions", "paper_trades",
          "paper_fills", "trades", "chat_history", "radar_tokens",
          "wallet_identity", "wallet_funding", "submitted_tokens",
          "errors"]

BATCH = 1000      # filas por lote al volcar; acota la memoria del backup


def make_backup() -> tuple[str, str, str]:
    """Genera el backup y devuelve (ruta_temporal, nombre_archivo, texto)."""
    stamp = time.strftime("%Y%m%d_%H%M")

    # tempfile.gettempdir(), no "/tmp" (auditoria 19/8): el bot local
    # corre en Windows, donde /tmp no existe y /backup moria con
    # "unable to open database file". El mismo codigo sirve en Railway.
    tmp = tempfile.gettempdir()
    if getattr(_db, "USE_PG", False):
        path = os.path.join(tmp, f"backup_{stamp}.json.gz")
        conn = _db.get_conn()
        filas = 0
        # Se escribe por lotes directamente al archivo. Antes se montaba
        # un diccionario con TODAS las filas de TODAS las tablas y luego
        # se volcaba: la base entera en memoria de golpe, cada 24 h. El
        # archivo que sale es identico; solo cambia como se construye.
        try:
            with gzip.open(path, "wt", encoding="utf-8",
                           compresslevel=6) as f:
                f.write("{")
                for i, t in enumerate(TABLES):
                    if i:
                        f.write(",")
                    f.write(json.dumps(t) + ":")
                    try:
                        cur = conn.execute(f"SELECT * FROM {t}")
                    except Exception as e:
                        # La consulta fallo antes de escribir nada de la
                        # tabla, asi que el JSON sigue siendo valido.
                        _avisar_ex("backup:make_backup:68", e)
                        f.write(json.dumps({"_error": str(e)}))
                        continue
                    f.write("[")
                    primero = True
                    while True:
                        lote = cur.fetchmany(BATCH)
                        if not lote:
                            break
                        for r in lote:
                            if not primero:
                                f.write(",")
                            f.write(json.dumps(dict(r), ensure_ascii=False,
                                               default=str))
                            primero = False
                        filas += len(lote)
                    f.write("]")
                f.write("}")
        finally:
            conn.close()
        mb = os.path.getsize(path) / 1024 / 1024
        cap = (f"💾 Backup (Postgres → JSON.gz) · {filas} filas · "
               f"{mb:.1f} MB · {stamp}\n"
               "Restaurar con:  python restaurar_backup.py <archivo>")
        if mb > 45:
            cap += ("\n⚠️ Cerca del límite de 50 MB de Telegram: baja "
                    "MAX_TRADES_TOTAL o excluye tablas grandes.")
        return path, f"wallets_backup_{stamp}.json.gz", cap

    # ── SQLite: copia consistente con la API de backup ──
    # (Ola 17-C) sqlite3.connect CREA el archivo si no existe: con una
    # DB_PATH mal puesta se generaba una base vacia, la copia pasaba el
    # PRAGMA integrity_check y todo salia verde. Un backup vacio
    # "verificado" es peor que ninguno, porque da falsa tranquilidad.
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"La base no existe en {DB_PATH!r}: no hay nada que respaldar. "
            f"Revisa DB_PATH o el directorio desde el que arranca el bot.")
    path = os.path.join(tmp, f"backup_{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)          # copia atómica, segura en caliente
        finally:
            dst.close()
    finally:
        src.close()
    mb = os.path.getsize(path) / 1024 / 1024
    # (19-AC, auditoria BAJO) integrity_check AQUI, sobre la copia, para
    # que tambien la verifique el /backup manual (antes solo el job
    # automatico lo hacia, en maintenance).
    try:
        chk_i = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            veredicto = chk_i.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk_i.close()
    except sqlite3.Error as e:
        veredicto = f"no se pudo comprobar ({e})"
    if veredicto != "ok":
        try:
            os.remove(path)
        except OSError:
            pass
        raise ValueError(f"La copia no pasa integrity_check: {veredicto}")
    # (Ola 17-C) Y una copia con 0 filas en las tablas del historico
    # tampoco cuenta como copia, aunque el archivo exista y sea valido.
    filas = 0
    try:
        chk = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for t in ("wallets", "appearances", "signals"):
                try:
                    filas += chk.execute(
                        f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    pass
        finally:
            chk.close()
    except sqlite3.Error:
        pass
    if filas == 0:
        try:                       # (Ola 17-E) sin dejar el temporal
            os.remove(path)
        except OSError:
            pass
        raise ValueError(
            "El backup salio VACIO (0 filas en wallets/appearances/"
            "signals). No se guarda: seria un respaldo inutil que ademas "
            "desplazaria a uno bueno en la rotacion.")
    cap = (f"💾 Backup SQLite · {mb:.1f} MB · {filas:,} filas del "
           f"histórico · {stamp}\n"
           "Ábrelo con cualquier visor de SQLite (o DB Browser). "
           "Para restaurar:  python restaurar_backup.py <archivo>")
    return path, f"wallets_backup_{stamp}.db", cap
