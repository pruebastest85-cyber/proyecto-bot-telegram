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
          "settings", "chat_history", "positions", "paper_trades",
          "predictions",
          # Tablas creadas de forma perezosa por sus módulos. Se migran
          # igual: 'trades' es el historial propio (lo más valioso) y
          # 'submitted_tokens' guarda tu feedback de qué tokens valen.
          "trades", "submitted_tokens", "wallet_identity",
          "wallet_funding", "errors",
          # (19-AF) Faltaban: paper_fills es el libro de idempotencia de
          # las ventas del paper (sin el, tras migrar se volverian a
          # aplicar) y radar_tokens el registro del radar.
          "paper_fills", "radar_tokens"]
SERIAL_TABLES = [("appearances", "id"), ("chat_history", "id"),
                 ("paper_trades", "id"), ("predictions", "id"),
                 ("paper_fills", "id")]

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

    # ── Marca de migración ya hecha ───────────────────────────────────────
    # Esto corre en CADA arranque del contenedor (Procfile). El .db de
    # SQLite lleva meses congelado, pero se volvían a empujar sus ~11.000
    # filas a Postgres en cada reinicio: no rompe nada (ON CONFLICT DO
    # NOTHING) pero resucita billeteras ya purgadas y alarga el arranque.
    # La huella es tamaño+fecha del archivo: si el .db no cambió, no hay
    # nada nuevo que migrar. Si algún día cambia, la migración se repite
    # sola, que es justo lo que se querría.
    huella = f"{size}:{int(os.path.getmtime(DB_PATH)) if exists else 0}"
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS settings "
                    "(key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("SELECT value FROM settings WHERE key='migracion_sqlite'")
        fila = cur.fetchone()
        if fila and fila[0] == huella:
            print(f"{TAG} el .db de SQLite no ha cambiado desde la última "
                  f"migración ({huella}): no hay nada que copiar.")
            pg.close()
            return
    except Exception as e:
        print(f"{TAG} no pude leer la marca de migración ({e}); migro igual.")
    # Las tablas perezosas no están en PG_SCHEMA: se crean aquí para que
    # la copia no se salte el historial propio ni el aprendizaje.
    for ddl in (
        """CREATE TABLE IF NOT EXISTS trades (
             wallet TEXT NOT NULL, signature TEXT NOT NULL, mint TEXT,
             side TEXT, sol DOUBLE PRECISION, tokens DOUBLE PRECISION,
             ts BIGINT, PRIMARY KEY (wallet, signature))""",
        """CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts
             ON trades (wallet, ts)""",
        """CREATE TABLE IF NOT EXISTS submitted_tokens (
             mint TEXT PRIMARY KEY, symbol TEXT, mc DOUBLE PRECISION,
             liq DOUBLE PRECISION, age_days DOUBLE PRECISION,
             top10_pct DOUBLE PRECISION, lp_locked_pct DOUBLE PRECISION,
             mint_auth INTEGER, freeze_auth INTEGER, risk_score INTEGER,
             smart_count INTEGER, elite_count INTEGER,
             chg24 DOUBLE PRECISION, feedback INTEGER,
             ts DOUBLE PRECISION)""",
        """CREATE TABLE IF NOT EXISTS wallet_identity (
             address TEXT PRIMARY KEY, tipo TEXT, nombre TEXT,
             categoria TEXT, etiquetas TEXT, ts DOUBLE PRECISION)""",
        """CREATE TABLE IF NOT EXISTS wallet_funding (
             address TEXT PRIMARY KEY, funder TEXT, funder_nombre TEXT,
             funder_tipo TEXT, monto DOUBLE PRECISION,
             ts_fondeo DOUBLE PRECISION, consultado DOUBLE PRECISION)""",
        """CREATE TABLE IF NOT EXISTS errors (
             ts DOUBLE PRECISION, modulo TEXT, tipo TEXT, mensaje TEXT)""",
    ):
        try:
            cur.execute(ddl)
        except Exception as e:
            print(f"{TAG} DDL perezoso omitido: {e}")
    # CRÍTICO: PG_SCHEMA no incluye las columnas añadidas después (grade,
    # consistency, hold_median_min, roi_median, price_at_buy, mc_at_buy,
    # entry_multiple…). Sin ellas, el INSERT de `wallets` y `appearances`
    # falla entero y NO se migra nada de esas tablas. db.get_conn() aplica
    # todas esas migraciones, así que se llama antes de copiar.
    try:
        _c = db.get_conn()
        try:
            pass
        finally:
            _c.close()
        print(f"{TAG} columnas añadidas al esquema Postgres.")
    except Exception as e:
        print(f"{TAG} aviso: no se pudieron aplicar migraciones de columnas: {e}")
    print(f"{TAG} esquema Postgres listo (incluidas tablas nuevas).")

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
        # Quedarse solo con las columnas que existen REALMENTE en Postgres:
        # así una columna nueva nunca vuelve a tumbar la migración entera.
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (t,))
            pg_cols = {r[0] for r in cur.fetchall()}
            if pg_cols:
                omitidas = [c for c in cols if c not in pg_cols]
                if omitidas:
                    print(f"{TAG} tabla {t}: columnas omitidas (no existen "
                          f"en Postgres): {omitidas}")
                cols = [c for c in cols if c in pg_cols]
        except Exception as e:
            print(f"{TAG} tabla {t}: no se pudo comprobar columnas ({e})")
        if not cols:
            print(f"{TAG} tabla {t}: sin columnas compatibles, se omite.")
            continue
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
    try:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('migracion_sqlite', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (huella,))
    except Exception as e:
        print(f"{TAG} no pude guardar la marca de migración: {e}")
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
