"""
Migración de datos SQLite → PostgreSQL (una sola vez).

Copia todo el contenido de la base SQLite actual (wallets.db, en la ruta
DB_PATH) a la base PostgreSQL indicada por DATABASE_URL. Es idempotente:
usa ON CONFLICT DO NOTHING, así que si lo corres dos veces no duplica.

Pensado para vivir en el Procfile antes del bot:
    worker: python migrate_to_pg.py && python telegram_bot.py
- Si NO hay DATABASE_URL  → no hace nada (el bot sigue en SQLite).
- Si hay DATABASE_URL     → crea el esquema y copia los datos a Postgres.
"""

import os
import sqlite3

from config import DB_PATH
import db

# Orden que respeta las dependencias (padres antes que hijos)
TABLES = ["winning_tokens", "wallets", "appearances", "signals",
          "settings", "chat_history"]

# Tablas con id autoincremental (hay que reajustar la secuencia al final)
SERIAL_TABLES = [("appearances", "id"), ("chat_history", "id")]


def main():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        # Sin Postgres configurado: no hay nada que migrar. Salida limpia
        # para que sea seguro dejar este script en el Procfile (el bot
        # seguirá usando SQLite con normalidad).
        print("ℹ️  DATABASE_URL no configurada: modo SQLite, sin migración.")
        return

    import psycopg2
    import psycopg2.extras

    pg = psycopg2.connect(database_url)
    pg.autocommit = True
    cur = pg.cursor()

    # 1) Crear el esquema en Postgres (idempotente)
    cur.execute(db.PG_SCHEMA)
    print("✅ Esquema Postgres listo.")

    if not os.path.exists(DB_PATH):
        print(f"ℹ️  No existe {DB_PATH}; no hay datos que migrar. "
              "Postgres queda vacío y el bot arrancará limpio.")
        return

    s = sqlite3.connect(DB_PATH)
    s.row_factory = sqlite3.Row

    total = 0
    for t in TABLES:
        try:
            rows = s.execute(f"SELECT * FROM {t}").fetchall()
        except sqlite3.OperationalError:
            print(f"· {t}: no existe en SQLite, se omite.")
            continue
        if not rows:
            print(f"· {t}: 0 filas.")
            continue
        cols = list(rows[0].keys())
        collist = ", ".join(cols)
        ph = ", ".join(["%s"] * len(cols))
        q = f"INSERT INTO {t} ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        data = [tuple(r[c] for c in cols) for r in rows]
        psycopg2.extras.execute_batch(cur, q, data, page_size=500)
        print(f"· {t}: {len(rows)} filas migradas.")
        total += len(rows)

    # 2) Reajustar las secuencias SERIAL para que los ids ya migrados no choquen
    for t, idcol in SERIAL_TABLES:
        seq_sql = ("SELECT setval(pg_get_serial_sequence(%s, %s), "
                   "COALESCE((SELECT MAX(" + idcol + ") FROM " + t + "), 1))")
        cur.execute(seq_sql, (t, idcol))

    s.close()
    pg.close()
    print(f"✅ Migración completa: {total} filas copiadas a Postgres.")


if __name__ == "__main__":
    main()
