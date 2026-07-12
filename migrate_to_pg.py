"""
Migración de datos SQLite → PostgreSQL (idempotente, con diagnóstico).

Copia el contenido de la base SQLite (DB_PATH) a la base PostgreSQL
(DATABASE_URL). Usa ON CONFLICT DO NOTHING, así que correrlo varias
veces no duplica. Imprime líneas 'MIGRACION_DIAG ...' para poder ver en
los logs exactamente qué encontró y cuántas filas copió.

Pensado para el Procfile antes del bot:
    worker: python migrate_to_pg.py && python telegram_bot.py
- Sin DATABASE_URL  → no hace nada (el bot sigue en SQLite).
- Con DATABASE_URL  → crea el esquema y copia los datos a Postgres.

NUNCA falla el arranque: cualquier error se registra y sale con código 0
para que el bot siempre pueda iniciar.
"""

import os
import sqlite3
import traceback

from config import DB_PATH
import db

TABLES = ["winning_tokens", "wallets", "appearances", "signals",
          "settings", "chat_history"]
SERIAL_TABLES = [("appearances", "id"), ("chat_history", "id")]

TAG = "MIGRACION_DIAG"


def _diag_volumen():
    """Muestra qué hay en el volumen para localizar el .db real."""
    for d in ("/data", os.path.dirname(DB_PATH) or "."):
        try:
            if os.path.isdir(d):
                print(f"{TAG} contenido de {d}: {os.listdir(d)}")
            else:
                print(f"{TAG} {d} no es directorio")
        except Exception as e:
            print(f"{TAG} no pude listar {d}: {e}")


def _run():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print(f"{TAG} DATABASE_URL no configurada: modo SQLite, sin migración.")
        return

    import psycopg2
    import psycopg2.extras

    exists = os.path.exists(DB_PATH)
    size = os.path.getsize(DB_PATH) if exists else 0
    print(f"{TAG} DB_PATH={DB_PATH!r} exists={exists} size={size} bytes")
    _diag_volumen()

    pg = psycopg2.connect(database_url)
    pg.autocommit = True
    cur = pg.cursor()
    cur.execute(db.PG_SCHEMA)
    print(f"{TAG} esquema Postgres listo.")

    if not exists or size == 0:
        print(f"{TAG} No hay SQLite con datos en {DB_PATH}. "
              "Nada que migrar por esta vía.")
        return

    s = sqlite3.connect(DB_PATH)
    s.row_factory = sqlite3.Row

    total = 0
    for t in TABLES:
        try:
            rows = s.execute(f"SELECT * FROM {t}").fetchall()
        except sqlite3.OperationalError as e:
            print(f"{TAG} tabla {t}: no existe en SQLite ({e}).")
            continue
        print(f"{TAG} tabla {t}: {len(rows)} filas en SQLite.")
        if not rows:
            continue
        cols = list(rows[0].keys())
        collist = ", ".join(cols)
        ph = ", ".join(["%s"] * len(cols))
        q = f"INSERT INTO {t} ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        data = [tuple(r[c] for c in cols) for r in rows]
        try:
            psycopg2.extras.execute_batch(cur, q, data, page_size=500)
            print(f"{TAG} tabla {t}: {len(rows)} filas enviadas a Postgres.")
            total += len(rows)
        except Exception as e:
            print(f"{TAG} tabla {t}: ERROR insertando: {e}")

    for t, idcol in SERIAL_TABLES:
        try:
            seq_sql = ("SELECT setval(pg_get_serial_sequence(%s, %s), "
                       "COALESCE((SELECT MAX(" + idcol + ") FROM " + t + "), 1))")
            cur.execute(seq_sql, (t, idcol))
        except Exception as e:
            print(f"{TAG} secuencia {t}: aviso: {e}")

    s.close()
    pg.close()
    print(f"{TAG} MIGRACIÓN COMPLETA: {total} filas procesadas.")


def main():
    try:
        _run()
    except Exception as e:
        print(f"{TAG} ERROR (no fatal, el bot arrancará igual): {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
