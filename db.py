"""
Capa de base de datos con SOPORTE DOBLE: SQLite o PostgreSQL.

Modo automático:
  - Si existe la variable de entorno DATABASE_URL  → usa PostgreSQL.
  - Si NO existe                                    → usa SQLite (como antes).

Así el bot sigue funcionando exactamente igual con SQLite, y si algo
falla en Postgres basta con quitar DATABASE_URL para volver atrás al
instante. Todo el SQL de la app usa placeholders '?' e 'INSERT OR
IGNORE' (estilo SQLite); en modo Postgres una capa fina los traduce a
'%s' y 'ON CONFLICT DO NOTHING', para no reescribir el resto de módulos.

Esquema:
  winning_tokens   → tokens que detectamos como ganadores y por qué
  wallets          → billeteras candidatas con sus métricas acumuladas
  appearances      → relación billetera↔token: la EVIDENCIA del porqué
  signals          → operaciones en tiempo real de billeteras ⭐
  settings         → configuración (umbral de señal, aprendizajes…)
  chat_history     → memoria del agente conversacional
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

# ──────────────────────────── ESQUEMAS ────────────────────────────────────

# Esquema SQLite (idéntico al histórico; las migraciones añaden columnas).
SCHEMA = """
CREATE TABLE IF NOT EXISTS winning_tokens (
    mint            TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    chain           TEXT DEFAULT 'solana',
    price_change_24h REAL,
    volume_24h_usd  REAL,
    liquidity_usd   REAL,
    pair_address    TEXT,
    detected_at     TEXT,
    analyzed        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    chain           TEXT DEFAULT 'solana',
    first_seen      TEXT,
    last_updated    TEXT,
    winning_tokens_count INTEGER DEFAULT 0,
    total_buys_sol  REAL DEFAULT 0,
    est_realized_sol REAL DEFAULT 0,
    label           TEXT,
    is_bot          INTEGER DEFAULT 0,
    is_tracked      INTEGER DEFAULT 0,
    score           REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS appearances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    buy_sol         REAL,
    buy_time        TEXT,
    buy_rank        INTEGER,
    est_pnl_sol     REAL,
    reason          TEXT,
    UNIQUE(wallet, mint),
    FOREIGN KEY(wallet) REFERENCES wallets(address),
    FOREIGN KEY(mint) REFERENCES winning_tokens(mint)
);

CREATE TABLE IF NOT EXISTS signals (
    signature       TEXT PRIMARY KEY,
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    sol             REAL,
    ts              INTEGER,
    side            TEXT DEFAULT 'compra'
);

CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    leader        TEXT NOT NULL,
    mint          TEXT NOT NULL,
    created_ts    INTEGER,
    stage         INTEGER DEFAULT 1,
    confidence    INTEGER,
    meta_score    INTEGER,
    predicted     TEXT,        -- JSON: seguidores esperados (wallet, prob, eta)
    arrived       TEXT,        -- JSON: seguidores que sí compraron
    alerted_stage INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'abierta',
    outcome_pct   REAL,
    token_chg_pct REAL,
    tier          TEXT,
    first_confirm_s INTEGER,
    price0        REAL,
    evaluated_ts  INTEGER,
    UNIQUE(leader, mint)
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    ts              TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    tokens          REAL DEFAULT 0,     -- tokens que tiene ahora
    sol_cost        REAL DEFAULT 0,     -- SOL gastado en lo que aún tiene
    realized_sol    REAL DEFAULT 0,     -- profit realizado acumulado (SOL)
    buys            INTEGER DEFAULT 0,
    sells           INTEGER DEFAULT 0,
    first_ts        INTEGER,
    last_ts         INTEGER,
    PRIMARY KEY (wallet, mint)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT,               -- señal que originó la operación
    wallet          TEXT,
    mint            TEXT,
    symbol          TEXT,
    stake_sol       REAL,               -- monto simulado (con tope)
    entry_price     REAL,
    entry_ts        INTEGER,
    exit_price      REAL,
    exit_ts         INTEGER,
    exit_reason     TEXT,               -- take-profit/stop-loss/tiempo/venta
    pnl_pct         REAL,
    pnl_sol         REAL,
    signal_score    REAL,
    status          TEXT DEFAULT 'abierta'
);

CREATE INDEX IF NOT EXISTS idx_signals_mint_ts ON signals(mint, ts);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score DESC);
CREATE INDEX IF NOT EXISTS idx_appearances_wallet ON appearances(wallet);
"""

# Esquema PostgreSQL: mismas tablas con TODAS las columnas ya incluidas
# (las que en SQLite se agregan por migración). REAL→DOUBLE PRECISION para
# no perder precisión de precios; AUTOINCREMENT→SERIAL; ts→BIGINT.
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS winning_tokens (
    mint             TEXT PRIMARY KEY,
    symbol           TEXT,
    name             TEXT,
    chain            TEXT DEFAULT 'solana',
    price_change_24h DOUBLE PRECISION,
    volume_24h_usd   DOUBLE PRECISION,
    liquidity_usd    DOUBLE PRECISION,
    pair_address     TEXT,
    detected_at      TEXT,
    analyzed         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallets (
    address          TEXT PRIMARY KEY,
    chain            TEXT DEFAULT 'solana',
    first_seen       TEXT,
    last_updated     TEXT,
    winning_tokens_count INTEGER DEFAULT 0,
    total_buys_sol   DOUBLE PRECISION DEFAULT 0,
    est_realized_sol DOUBLE PRECISION DEFAULT 0,
    label            TEXT,
    is_bot           INTEGER DEFAULT 0,
    is_tracked       INTEGER DEFAULT 0,
    score            DOUBLE PRECISION DEFAULT 0,
    ai_class         TEXT,
    ai_follow        INTEGER,
    ai_reason        TEXT,
    alias            TEXT,
    pnl_30d          DOUBLE PRECISION,
    pnl_total        DOUBLE PRECISION,
    pnl_unreal       DOUBLE PRECISION,
    pnl_net          DOUBLE PRECISION,
    pnl_updated      TEXT,
    wallet_score     DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS appearances (
    id              SERIAL PRIMARY KEY,
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    buy_sol         DOUBLE PRECISION,
    buy_time        TEXT,
    buy_rank        INTEGER,
    est_pnl_sol     DOUBLE PRECISION,
    reason          TEXT,
    UNIQUE(wallet, mint)
);

CREATE TABLE IF NOT EXISTS signals (
    signature       TEXT PRIMARY KEY,
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    sol             DOUBLE PRECISION,
    ts              BIGINT,
    side            TEXT DEFAULT 'compra',
    price_usd       DOUBLE PRECISION,
    price_1h        DOUBLE PRECISION,
    price_24h       DOUBLE PRECISION,
    chg_1h          DOUBLE PRECISION,
    chg_24h         DOUBLE PRECISION,
    alerted_pct     DOUBLE PRECISION DEFAULT 0,
    symbol          TEXT,
    mc              DOUBLE PRECISION,
    liq             DOUBLE PRECISION,
    signal_score    DOUBLE PRECISION,
    verdict         TEXT,
    alerted         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS predictions (
    id            SERIAL PRIMARY KEY,
    leader        TEXT NOT NULL,
    mint          TEXT NOT NULL,
    created_ts    BIGINT,
    stage         INTEGER DEFAULT 1,
    confidence    INTEGER,
    meta_score    INTEGER,
    predicted     TEXT,
    arrived       TEXT,
    alerted_stage INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'abierta',
    outcome_pct   DOUBLE PRECISION,
    token_chg_pct DOUBLE PRECISION,
    tier          TEXT,
    first_confirm_s INTEGER,
    price0        DOUBLE PRECISION,
    evaluated_ts  BIGINT,
    UNIQUE(leader, mint)
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id              SERIAL PRIMARY KEY,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    ts              TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    wallet          TEXT NOT NULL,
    mint            TEXT NOT NULL,
    tokens          DOUBLE PRECISION DEFAULT 0,
    sol_cost        DOUBLE PRECISION DEFAULT 0,
    realized_sol    DOUBLE PRECISION DEFAULT 0,
    buys            INTEGER DEFAULT 0,
    sells           INTEGER DEFAULT 0,
    first_ts        BIGINT,
    last_ts         BIGINT,
    PRIMARY KEY (wallet, mint)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id              SERIAL PRIMARY KEY,
    signature       TEXT,
    wallet          TEXT,
    mint            TEXT,
    symbol          TEXT,
    stake_sol       DOUBLE PRECISION,
    entry_price     DOUBLE PRECISION,
    entry_ts        BIGINT,
    exit_price      DOUBLE PRECISION,
    exit_ts         BIGINT,
    exit_reason     TEXT,
    pnl_pct         DOUBLE PRECISION,
    pnl_sol         DOUBLE PRECISION,
    signal_score    DOUBLE PRECISION,
    status          TEXT DEFAULT 'abierta'
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);

CREATE INDEX IF NOT EXISTS idx_signals_mint_ts ON signals(mint, ts);
CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score DESC);
CREATE INDEX IF NOT EXISTS idx_appearances_wallet ON appearances(wallet);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────── Capa de compatibilidad para PostgreSQL ───────────────────

def _translate(sql: str) -> str:
    """Traduce SQL estilo SQLite a PostgreSQL:
       'INSERT OR IGNORE' → 'INSERT ... ON CONFLICT DO NOTHING'
       placeholders '?'   → '%s'
    """
    ignore = re.search(r'INSERT\s+OR\s+IGNORE', sql, flags=re.I) is not None
    q = re.sub(r'INSERT\s+OR\s+IGNORE', 'INSERT', sql, flags=re.I)
    q = q.replace('?', '%s')
    if ignore:
        q = q.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    return q


class _PgCursor:
    """Cursor mínimo compatible con el uso que hace la app (fetchone,
    fetchall, rowcount, iteración)."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        return iter(self._cur.fetchall())


class _PgConn:
    """Envoltura sobre una conexión psycopg2 que imita el subconjunto de la
    API de sqlite3.Connection que usa la app: execute(), executescript(),
    commit(), close(). Funciona en autocommit para evitar transacciones
    colgadas y replicar el comportamiento de SQLite."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        import psycopg2.extras
        cur = self._conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor)
        q = _translate(sql)
        if params:
            cur.execute(q, tuple(params))
        else:
            cur.execute(q)
        return _PgCursor(cur)

    def executescript(self, script: str):
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def commit(self):
        # autocommit activado; no-op para compatibilidad.
        pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ──────────────────────────── CONEXIÓN ────────────────────────────────────

def _dedupe_aliases(conn):
    """Limpieza de apodos duplicados heredados: la billetera más antigua
    conserva el nombre limpio; a las ⭐ activas se les borra el apodo para
    que la IA les invente uno nuevo y único; el resto recibe sufijo.
    Idempotente; ignora bots descartados."""
    try:
        dups = conn.execute(
            """SELECT alias FROM wallets
               WHERE alias IS NOT NULL AND COALESCE(is_bot, 0) = 0
               GROUP BY alias HAVING COUNT(*) > 1""").fetchall()
        for d in dups:
            rows = conn.execute(
                """SELECT address FROM wallets WHERE alias = ?
                   ORDER BY COALESCE(first_seen, ''), address""",
                (d["alias"],)).fetchall()
            for r in rows[1:]:
                w = conn.execute(
                    "SELECT is_tracked FROM wallets WHERE address = ?",
                    (r["address"],)).fetchone()
                if w and w["is_tracked"]:
                    # ⭐ activa: se borra el apodo y la IA le inventará
                    # uno nuevo y único en el próximo ciclo
                    conn.execute(
                        "UPDATE wallets SET alias = NULL WHERE address = ?",
                        (r["address"],))
                else:
                    conn.execute(
                        "UPDATE wallets SET alias = ? WHERE address = ?",
                        (f"{d['alias']} ({r['address'][:4]})", r["address"]))
            if rows[1:]:
                print(f"· Alias duplicado: {d['alias']} → "
                      f"{len(rows) - 1} se renombrarán")
        if dups:
            conn.commit()
    except Exception as e:
        print(f"· Dedupe de alias omitido: {e}")


def get_conn():
    """Devuelve una conexión lista para usar (SQLite o Postgres) con el
    esquema ya creado. La interfaz es la misma en ambos modos."""
    if USE_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        pg = _PgConn(conn)
        pg.executescript(PG_SCHEMA)
        # Migraciones idempotentes para bases ya existentes (Postgres soporta
        # ADD COLUMN IF NOT EXISTS).
        for tbl, col, typ in [
                ("predictions", "tier", "TEXT"),
                ("predictions", "first_confirm_s", "INTEGER"),
                ("predictions", "price0", "DOUBLE PRECISION"),
                ("wallets", "pnl_unreal", "DOUBLE PRECISION"),
                ("wallets", "pnl_net", "DOUBLE PRECISION")]:
            try:
                pg.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS "
                           f"{col} {typ}")
            except Exception:
                pass
        _dedupe_aliases(pg)
        return pg

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migraciones (ignorar si la columna ya existe)
    for col, typ in [("ai_class", "TEXT"), ("ai_follow", "INTEGER"),
                     ("ai_reason", "TEXT"), ("alias", "TEXT"),
                     ("pnl_30d", "REAL"), ("pnl_total", "REAL"),
                     ("pnl_unreal", "REAL"), ("pnl_net", "REAL"),
                     ("pnl_updated", "TEXT"), ("wallet_score", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    for col, typ in [("side", "TEXT DEFAULT 'compra'"),
                     ("price_usd", "REAL"), ("price_1h", "REAL"),
                     ("price_24h", "REAL"), ("chg_1h", "REAL"),
                     ("chg_24h", "REAL"), ("alerted_pct", "REAL DEFAULT 0"),
                     ("symbol", "TEXT"), ("mc", "REAL"), ("liq", "REAL"),
                     ("signal_score", "REAL"), ("verdict", "TEXT"),
                     ("alerted", "INTEGER DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    # Tabla predictions (motor predictivo): columnas añadidas después de
    # su creación inicial.
    for col, typ in [("stage", "INTEGER DEFAULT 1"), ("confidence", "INTEGER"),
                     ("meta_score", "INTEGER"), ("predicted", "TEXT"),
                     ("arrived", "TEXT"), ("alerted_stage", "INTEGER DEFAULT 0"),
                     ("status", "TEXT DEFAULT 'abierta'"), ("outcome_pct", "REAL"),
                     ("token_chg_pct", "REAL"), ("tier", "TEXT"),
                     ("first_confirm_s", "INTEGER"), ("price0", "REAL"),
                     ("evaluated_ts", "INTEGER")]:
        try:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    _dedupe_aliases(conn)
    return conn


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value):
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, str(value)))
    conn.commit()


# ── Posiciones (tokens que tiene cada billetera y su profit) ──────────────

def get_position(conn, wallet: str, mint: str):
    return conn.execute(
        "SELECT * FROM positions WHERE wallet=? AND mint=?",
        (wallet, mint)).fetchone()


def apply_buy(conn, wallet: str, mint: str, sol: float, tokens: float,
              ts: int) -> dict:
    """Registra una compra y devuelve info para la alerta (incluye si es
    acumulación: ya tenía tokens de ese mint)."""
    sol = sol or 0.0
    tokens = tokens or 0.0
    row = get_position(conn, wallet, mint)
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO positions (wallet, mint, first_ts) "
            "VALUES (?,?,?)", (wallet, mint, ts))
        tokens0 = cost0 = 0.0
        buys0 = 0
    else:
        tokens0 = row["tokens"] or 0.0
        cost0 = row["sol_cost"] or 0.0
        buys0 = row["buys"] or 0
    is_accum = buys0 >= 1 or tokens0 > 0
    tokens_new = tokens0 + tokens
    cost_new = cost0 + sol
    buys_new = buys0 + 1
    conn.execute(
        "UPDATE positions SET tokens=?, sol_cost=?, buys=?, last_ts=? "
        "WHERE wallet=? AND mint=?",
        (tokens_new, cost_new, buys_new, ts, wallet, mint))
    conn.commit()
    return {"tokens_bought": tokens, "total_tokens": tokens_new,
            "buys": buys_new, "is_accumulation": is_accum,
            "sol_invertido": cost_new}


def apply_sell(conn, wallet: str, mint: str, sol: float, tokens: float,
               ts: int) -> dict:
    """Registra una venta y calcula profit realizado, % vendido y remanente.
    Si no habíamos visto la compra, marca known=False."""
    sol = sol or 0.0
    tokens = tokens or 0.0
    row = get_position(conn, wallet, mint)
    tokens0 = (row["tokens"] if row else 0.0) or 0.0
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO positions (wallet, mint, first_ts) "
            "VALUES (?,?,?)", (wallet, mint, ts))
    if tokens0 <= 0:
        # No conocemos su compra: registramos la venta sin PnL fiable.
        conn.execute(
            "UPDATE positions SET sells=COALESCE(sells,0)+1, last_ts=? "
            "WHERE wallet=? AND mint=?", (ts, wallet, mint))
        conn.commit()
        return {"known": False, "tokens_sold": tokens, "proceeds": sol,
                "realized_this": None, "realized_total": None,
                "remaining_tokens": None, "pct_sold": None,
                "fully_sold": None}
    cost0 = row["sol_cost"] or 0.0
    real0 = row["realized_sol"] or 0.0
    sells0 = row["sells"] or 0
    frac = min(1.0, tokens / tokens0) if tokens0 > 0 else 1.0
    cost_of_sold = cost0 * frac
    realized_this = sol - cost_of_sold
    tokens_left = max(0.0, tokens0 - tokens)
    cost_left = max(0.0, cost0 - cost_of_sold)
    realized_total = real0 + realized_this
    fully = tokens_left <= tokens0 * 0.02          # ≤2% restante = vendió todo
    conn.execute(
        "UPDATE positions SET tokens=?, sol_cost=?, realized_sol=?, "
        "sells=?, last_ts=? WHERE wallet=? AND mint=?",
        (tokens_left, cost_left, realized_total, sells0 + 1, ts, wallet, mint))
    conn.commit()
    return {"known": True, "tokens_sold": tokens, "proceeds": sol,
            "realized_this": realized_this, "realized_total": realized_total,
            "remaining_tokens": tokens_left, "pct_sold": frac * 100,
            "fully_sold": fully}


def wallet_positions(conn, wallet: str, limit: int = 25):
    return conn.execute(
        "SELECT mint, tokens, sol_cost, realized_sol, buys, sells, last_ts "
        "FROM positions WHERE wallet=? ORDER BY last_ts DESC LIMIT ?",
        (wallet, limit)).fetchall()


def wallet_positions_summary(conn, wallet: str):
    """Resumen de posiciones/transacciones de una billetera (para la IA)."""
    rows = wallet_positions(conn, wallet, 30)
    if not rows:
        return None
    abiertas = [r for r in rows if (r["tokens"] or 0) > 0]
    realizado = sum((r["realized_sol"] or 0) for r in rows)
    invertido_abierto = sum((r["sol_cost"] or 0) for r in abiertas)
    return {
        "posiciones_totales": len(rows),
        "posiciones_abiertas": len(abiertas),
        "profit_realizado_sol": round(realizado, 2),
        "sol_invertido_en_abiertas": round(invertido_abierto, 2),
        "detalle": [
            {"mint": r["mint"],
             "tokens_actuales": r["tokens"],
             "sol_invertido": round(r["sol_cost"] or 0, 3),
             "profit_realizado_sol": round(r["realized_sol"] or 0, 2),
             "sigue_dentro": (r["tokens"] or 0) > 0,
             "compras": r["buys"], "ventas": r["sells"]}
            for r in rows[:12]
        ],
    }


# ── Tokens ganadores ──────────────────────────────────────────────────────

def save_winning_token(conn, token: dict):
    conn.execute(
        """INSERT OR IGNORE INTO winning_tokens
           (mint, symbol, name, price_change_24h, volume_24h_usd,
            liquidity_usd, pair_address, detected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (token["mint"], token.get("symbol"), token.get("name"),
         token.get("price_change_24h"), token.get("volume_24h_usd"),
         token.get("liquidity_usd"), token.get("pair_address"), now_iso()),
    )
    conn.commit()


def pending_tokens(conn):
    return conn.execute(
        "SELECT * FROM winning_tokens WHERE analyzed = 0"
    ).fetchall()


def mark_analyzed(conn, mint: str):
    conn.execute("UPDATE winning_tokens SET analyzed = 1 WHERE mint = ?", (mint,))
    conn.commit()


# ── Billeteras y apariciones ─────────────────────────────────────────────

def upsert_wallet_appearance(conn, wallet: str, mint: str, buy_sol: float,
                             buy_time: str, buy_rank: int, reason: str):
    conn.execute(
        """INSERT OR IGNORE INTO wallets (address, first_seen, last_updated)
           VALUES (?,?,?)""",
        (wallet, now_iso(), now_iso()),
    )
    cur = conn.execute(
        """INSERT OR IGNORE INTO appearances
           (wallet, mint, buy_sol, buy_time, buy_rank, reason)
           VALUES (?,?,?,?,?,?)""",
        (wallet, mint, buy_sol, buy_time, buy_rank, reason),
    )
    if cur.rowcount:  # solo si la aparición es nueva
        conn.execute(
            """UPDATE wallets SET
                 winning_tokens_count = (
                     SELECT COUNT(*) FROM appearances WHERE wallet = ?),
                 total_buys_sol = total_buys_sol + ?,
                 last_updated = ?
               WHERE address = ?""",
            (wallet, buy_sol, now_iso(), wallet),
        )
    conn.commit()


def recompute_scores(conn, min_winning_tokens: int):
    """
    Puntaje simple para la fase 1:
      apariciones en tokens ganadores (peso fuerte)
      + qué tan temprano compra en promedio (peso medio)
    Las billeteras que superan el umbral pasan a is_tracked = 1.
    Las marcadas como bot (incluye descartes manuales) quedan fuera.
    """
    conn.execute(
        """UPDATE wallets SET score =
             winning_tokens_count * 10.0
             + COALESCE((SELECT AVG(100.0 / (buy_rank + 1))
                         FROM appearances WHERE wallet = address), 0)
           WHERE is_bot = 0"""
    )
    conn.execute(
        "UPDATE wallets SET is_tracked = 1 WHERE winning_tokens_count >= ? AND is_bot = 0",
        (min_winning_tokens,),
    )
    conn.commit()


def top_wallets(conn, limit=20):
    # Primero las ⭐ rastreadas, ordenadas por su calidad real (Wallet Score
    # 0-100 de la IA); luego el resto de candidatas por el score de
    # descubrimiento. Así las mejores probadas quedan siempre arriba.
    return conn.execute(
        """SELECT address, winning_tokens_count, total_buys_sol, score, is_tracked,
                  ai_class, alias, pnl_30d, pnl_total, wallet_score
           FROM wallets WHERE is_bot = 0
           ORDER BY is_tracked DESC,
                    CASE WHEN wallet_score IS NULL THEN 1 ELSE 0 END,
                    wallet_score DESC,
                    score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def wallet_evidence(conn, wallet: str):
    """Devuelve el 'porqué' completo de una billetera: cada aparición con su razón."""
    return conn.execute(
        """SELECT a.mint, t.symbol, a.buy_sol, a.buy_rank, a.buy_time, a.reason
           FROM appearances a
           LEFT JOIN winning_tokens t ON t.mint = a.mint
           WHERE a.wallet = ? ORDER BY a.buy_time""",
        (wallet,),
    ).fetchall()
