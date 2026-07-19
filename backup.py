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

import json
import os
import sqlite3
import time

import db as _db
from config import DB_PATH

TABLES = ["wallets", "winning_tokens", "appearances", "signals",
          "settings", "predictions"]


def make_backup() -> tuple[str, str, str]:
    """Genera el backup y devuelve (ruta_temporal, nombre_archivo, texto)."""
    stamp = time.strftime("%Y%m%d_%H%M")

    if getattr(_db, "USE_PG", False):
        path = f"/tmp/backup_{stamp}.json"
        conn = _db.get_conn()
        data, filas = {}, 0
        try:
            for t in TABLES:
                try:
                    rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                    data[t] = [dict(r) for r in rows]
                    filas += len(data[t])
                except Exception as e:
                    data[t] = {"_error": str(e)}
        finally:
            conn.close()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        cap = (f"💾 Backup (Postgres → JSON) · {filas} filas · {stamp}\n"
               "Guárdalo en sitio seguro. Restaurable con un script de import.")
        return path, f"wallets_backup_{stamp}.json", cap

    # ── SQLite: copia consistente con la API de backup ──
    path = f"/tmp/backup_{stamp}.db"
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
