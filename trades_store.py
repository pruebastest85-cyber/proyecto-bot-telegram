"""
Historial propio de operaciones.

EL PROBLEMA QUE RESUELVE
------------------------
Al perfilar una billetera se descargan miles de transacciones de Helius, se
calculan las métricas… y se tira todo. A los 3 días, en la re-evaluación, se
vuelve a descargar exactamente lo mismo. Eso significa:

  · pagar dos veces por el mismo dato,
  · no poder recalcular nada si mejoramos una fórmula,
  · y depender de Helius para siempre.

Aquí se guarda cada COMPRA y VENTA detectada. No la transacción cruda (que
ocupa muchísimo), sino lo que de verdad importa: quién, qué token, si compró
o vendió, cuánto SOL, cuántos tokens y cuándo.

Con el tiempo el bot acumula su propio historial: puede recalcular métricas
sin llamar a nadie, y si un día Helius falla o se encarece, los datos ya son
tuyos.

CONTROL DE TAMAÑO
-----------------
La base se envía por Telegram como backup, así que no puede crecer sin
límite: se conservan como máximo MAX_TRADES_POR_WALLET operaciones por
billetera (las más recientes) y se podan las viejas.
"""

import os
import time

from db import get_conn


def _int_env(n, d):
    try:
        return int(os.getenv(n, d))
    except (TypeError, ValueError):
        return d


MAX_TRADES_POR_WALLET = _int_env("MAX_TRADES_POR_WALLET", 3000)
# Tope GLOBAL: la base viaja en el backup diario de Telegram (límite 50 MB),
# así que el historial no puede crecer sin freno. Al llegar al tope se
# eliminan primero las operaciones más antiguas de billeteras que NO son ⭐
# ni están bien graduadas: se conserva lo que de verdad aporta.
# En Postgres el archivo no viaja por Telegram (el backup es un volcado
# JSON), así que el historial puede ser mucho mayor. En SQLite el backup va
# comprimido, lo que ya multiplica varias veces el margen.
_POR_DEFECTO = 5_000_000 if os.getenv("DATABASE_URL", "").strip() else 2_000_000
MAX_TRADES_TOTAL = _int_env("MAX_TRADES_TOTAL", _POR_DEFECTO)


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
                      wallet TEXT NOT NULL,
                      signature TEXT NOT NULL,
                      mint TEXT,
                      side TEXT,
                      sol REAL,
                      tokens REAL,
                      ts INTEGER,
                      PRIMARY KEY (wallet, signature))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts "
                 "ON trades (wallet, ts)")
    conn.commit()


def guardar(wallet: str, operaciones: list[dict]) -> int:
    """
    Guarda las operaciones de una billetera. Idempotente: repetir la misma
    firma no duplica nada. Devuelve cuántas eran NUEVAS.
    """
    if not wallet or not operaciones:
        return 0
    nuevas = 0
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            for o in operaciones:
                sig = o.get("signature")
                if not sig:
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO trades
                       (wallet, signature, mint, side, sol, tokens, ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (wallet, sig, o.get("mint"), o.get("side"),
                     o.get("sol"), o.get("tokens"), o.get("ts")))
                if cur.rowcount:
                    nuevas += 1
            conn.commit()
            _podar(conn, wallet)
            _podar_global(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"· No se pudieron guardar operaciones: {e}")
    return nuevas


def _podar(conn, wallet: str) -> None:
    """Conserva solo las más recientes: la base viaja en el backup diario."""
    try:
        n = conn.execute("SELECT COUNT(*) c FROM trades WHERE wallet=?",
                         (wallet,)).fetchone()["c"]
        if n <= MAX_TRADES_POR_WALLET:
            return
        conn.execute(
            """DELETE FROM trades WHERE wallet=? AND signature IN (
                 SELECT signature FROM trades WHERE wallet=?
                 ORDER BY ts ASC LIMIT ?)""",
            (wallet, wallet, n - MAX_TRADES_POR_WALLET))
        conn.commit()
    except Exception:
        pass


def _podar_global(conn) -> int:
    """Si se supera el tope global, sacrifica primero lo menos valioso."""
    try:
        total = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
        if total <= MAX_TRADES_TOTAL:
            return 0
        sobran = total - MAX_TRADES_TOTAL
        # Primero: lo más viejo de billeteras sin ⭐ ni buen grado
        conn.execute(
            """DELETE FROM trades WHERE rowid IN (
                 SELECT t.rowid FROM trades t
                 LEFT JOIN wallets w ON w.address = t.wallet
                 WHERE COALESCE(w.is_tracked,0)=0
                   AND COALESCE(w.grade,'') NOT IN ('Elite','Seguimiento')
                 ORDER BY t.ts ASC LIMIT ?)""", (sobran,))
        conn.commit()
        restante = conn.execute(
            "SELECT COUNT(*) c FROM trades").fetchone()["c"]
        if restante > MAX_TRADES_TOTAL:
            # Aún sobra: recortar lo más antiguo, sea de quien sea
            conn.execute(
                """DELETE FROM trades WHERE rowid IN (
                     SELECT rowid FROM trades ORDER BY ts ASC LIMIT ?)""",
                (restante - MAX_TRADES_TOTAL,))
            conn.commit()
        return total - conn.execute(
            "SELECT COUNT(*) c FROM trades").fetchone()["c"]
    except Exception:
        return 0


def ultimo_ts(wallet: str) -> int:
    """Momento de la operación más reciente que ya tenemos guardada.
    Sirve para, en el futuro, pedir a Helius SOLO lo nuevo."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            r = conn.execute("SELECT MAX(ts) m FROM trades WHERE wallet=?",
                             (wallet,)).fetchone()
            return int(r["m"] or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def cargar(wallet: str, limite: int = 5000) -> list[dict]:
    """Las operaciones guardadas de una billetera, de más nueva a más vieja."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            filas = conn.execute(
                """SELECT signature, mint, side, sol, tokens, ts
                   FROM trades WHERE wallet=? ORDER BY ts DESC LIMIT ?""",
                (wallet, limite)).fetchall()
            return [dict(f) for f in filas]
        finally:
            conn.close()
    except Exception:
        return []


def metricas_locales(wallet: str) -> dict | None:
    """
    Métricas calculadas SOLO con datos propios, sin llamar a Helius.

    Es el respaldo real: si la API falla, se agota la cuota o algún día se
    deja de usar, el bot sigue pudiendo evaluar con lo que ya guardó.
    """
    ops = cargar(wallet)
    if len(ops) < 4:
        return None
    por_token: dict[str, dict] = {}
    for o in ops:
        m = o.get("mint")
        if not m:
            continue
        d = por_token.setdefault(m, {"sol_in": 0.0, "sol_out": 0.0,
                                     "buys": 0, "sells": 0})
        sol = float(o.get("sol") or 0)
        if o.get("side") == "compra":
            d["sol_out"] += sol
            d["buys"] += 1
        else:
            d["sol_in"] += sol
            d["sells"] += 1
    pnl = sum(d["sol_in"] - d["sol_out"] for d in por_token.values())
    cerradas = [d for d in por_token.values() if d["sells"] > 0]
    ganadoras = [d for d in cerradas if d["sol_in"] - d["sol_out"] > 0]
    ts = [o["ts"] for o in ops if o.get("ts")]
    return {
        "origen": "datos propios",
        "operaciones": len(ops),
        "tokens": len(por_token),
        "pnl_sol": round(pnl, 2),
        "cerradas": len(cerradas),
        "win_rate_pct": (round(100 * len(ganadoras) / len(cerradas))
                         if cerradas else None),
        "primera": min(ts) if ts else None,
        "ultima": max(ts) if ts else None,
    }


def estadisticas() -> dict:
    """Cuánta historia propia llevamos acumulada."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            r = conn.execute(
                "SELECT COUNT(*) ops, COUNT(DISTINCT wallet) w, "
                "COUNT(DISTINCT mint) m, MIN(ts) desde FROM trades"
            ).fetchone()
            return {"operaciones": r["ops"] or 0, "billeteras": r["w"] or 0,
                    "tokens": r["m"] or 0, "desde": r["desde"]}
        finally:
            conn.close()
    except Exception:
        return {"operaciones": 0, "billeteras": 0, "tokens": 0, "desde": None}


def resumen_text() -> str:
    e = estadisticas()
    if not e["operaciones"]:
        return ("📚 *Historial propio*\n\nAún sin operaciones guardadas. "
                "Se irán acumulando en cada ciclo de perfilado.")
    out = ["📚 *Historial propio* (independiente de Helius)\n",
           f"Operaciones guardadas: *{e['operaciones']:,}*",
           f"Billeteras con historia: *{e['billeteras']:,}*",
           f"Tokens distintos: *{e['tokens']:,}*"]
    if e["desde"]:
        dias = (time.time() - float(e["desde"])) / 86400
        out.append(f"Cubre desde hace *{dias:.0f} días*")
    out.append(f"\n_Topes: {MAX_TRADES_POR_WALLET:,} por billetera · "
               f"{MAX_TRADES_TOTAL:,} en total (para que el backup diario "
               "siga cabiendo en Telegram)._")
    return "\n".join(out)
