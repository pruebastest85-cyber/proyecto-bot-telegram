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

TABLES = ["wallets", "winning_tokens", "appearances", "signals",
          "settings", "predictions", "positions", "paper_trades",
          "chat_history"]

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
    cap = (f"💾 Backup SQLite · {mb:.1f} MB · {stamp}\n"
           "Ábrelo con cualquier visor de SQLite (o DB Browser). "
           "Para restaurar, súbelo como wallets.db a /data.")
    return path, f"wallets_backup_{stamp}.db", cap
