"""
Capa de base de datos (SQLite para la fase de validación).
Cuando el sistema crezca, migrar a PostgreSQL es directo porque
todo el SQL está concentrado aquí.

Esquema:
  winning_tokens   → tokens que detectamos como ganadores y por qué
  wallets          → billeteras candidatas con sus métricas acumuladas
  appearances      → relación billetera↔token: la EVIDENCIA del porqué
  signals          → operaciones en tiempo real de billeteras ⭐
"""

import sqlite3
from datetime import datetime, timezone
from config import DB_PATH

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

CREATE INDEX IF NOT EXISTS idx_signals_mint_ts ON signals(mint, ts);
CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score DESC);
CREATE INDEX IF NOT EXISTS idx_appearances_wallet ON appearances(wallet);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migraciones (ignorar si la columna ya existe)
    for col, typ in [("ai_class", "TEXT"), ("ai_follow", "INTEGER"),
                     ("ai_reason", "TEXT"), ("alias", "TEXT"),
                     ("pnl_30d", "REAL"), ("pnl_total", "REAL"),
                     ("pnl_updated", "TEXT"), ("wallet_score", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    for col, typ in [("side", "TEXT DEFAULT 'compra'"),
                     ("price_usd", "REAL"), ("price_1h", "REAL"),
                     ("price_24h", "REAL"), ("chg_1h", "REAL"),
                     ("chg_24h", "REAL"), ("alerted_pct", "REAL DEFAULT 0"),
                     ("symbol", "TEXT"), ("mc", "REAL"), ("liq", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


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
    return conn.execute(
        """SELECT address, winning_tokens_count, total_buys_sol, score, is_tracked,
                  ai_class, alias, pnl_30d, pnl_total, wallet_score
           FROM wallets WHERE is_bot = 0
           ORDER BY score DESC LIMIT ?""",
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
