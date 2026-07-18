"""
Paper trading: simula operaciones con las señales alertadas para medir
si el sistema ganaría dinero SIN arriesgar capital real.

Reglas:
  - Cada señal de COMPRA alertada abre una posición simulada. El monto
    es el de la billetera ⭐, con TOPE configurable por señal
    (settings 'paper_max_sol').
  - Solo una posición abierta por token, y un máximo de posiciones
    abiertas a la vez ('paper_max_abiertas').
  - Cierre por: take-profit ('paper_tp_pct', +100% por defecto),
    stop-loss ('paper_sl_pct', 50 → cierra a -50%), tiempo
    ('paper_timeout_h', 48 h), o porque la billetera ⭐ que dio la
    señal vendió (seguimos su salida).
  - PnL nocional en SOL: pnl = monto × variación % del precio USD.
    No simula slippage ni fees; es una medida optimista pero comparable.

Settings (tabla settings, editables con /paper o desde el chat):
  paper_enabled       "1"/"0"  (default 1)
  paper_max_sol       tope de monto por señal, en SOL (default 1.0)
  paper_max_abiertas  máximo de posiciones abiertas (default 10)
  paper_tp_pct        take-profit % (default 100 = x2)
  paper_sl_pct        stop-loss % positivo (default 50 → -50%)
  paper_timeout_h     horas máximas de una posición (default 48)
"""

import time

from db import get_conn, get_setting

HOUR = 3600


def _f(conn, key: str, default: float) -> float:
    try:
        return float(get_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _enabled(conn) -> bool:
    return (get_setting(conn, "paper_enabled", "1") or "1").strip() != "0"


def _tg(text: str):
    try:
        from realtime import tg_send
        tg_send(text)
    except Exception as e:
        print(f"· Paper: aviso TG falló: {e}")


# ───────────────────────── Apertura ───────────────────────────────────────

def open_trade(conn, trade: dict, token: dict, score) -> bool:
    """Abre una posición simulada a partir de una señal de compra alertada.
    Devuelve True si se abrió."""
    if not _enabled(conn):
        return False
    price = token.get("price")
    if not price or price <= 0:
        print("· Paper: sin precio del token; no se abre posición")
        return False

    # Una posición abierta por token
    ya = conn.execute(
        "SELECT id FROM paper_trades WHERE mint=? AND status='abierta'",
        (trade["mint"],)).fetchone()
    if ya:
        return False

    # Máximo de posiciones abiertas
    max_abiertas = int(_f(conn, "paper_max_abiertas", 10))
    n = conn.execute(
        "SELECT COUNT(*) c FROM paper_trades WHERE status='abierta'"
    ).fetchone()["c"]
    if n >= max_abiertas:
        print(f"· Paper: {n} posiciones abiertas (máx {max_abiertas}); "
              "no se abre otra")
        return False

    # Tope de monto por señal
    max_sol = _f(conn, "paper_max_sol", 1.0)
    stake = min(float(trade.get("sol") or 0) or max_sol, max_sol)
    if stake <= 0:
        return False

    sym = token.get("symbol") or trade["mint"][:6]
    conn.execute(
        """INSERT INTO paper_trades
           (signature, wallet, mint, symbol, stake_sol, entry_price,
            entry_ts, signal_score, status)
           VALUES (?,?,?,?,?,?,?,?, 'abierta')""",
        (trade["signature"], trade["wallet"], trade["mint"], sym,
         stake, price, trade["ts"], score))
    conn.commit()
    print(f"🧪 Paper: compra simulada {stake:.2f} SOL en {sym} @ ${price:.6g}")
    _tg(f"🧪 *Paper:* compra simulada de *{stake:.2f} SOL* en *{sym}* "
        f"@ ${price:.6g}  ({n + 1}/{max_abiertas} abiertas)\nVer: /paper")
    return True


# ───────────────────────── Cierres ────────────────────────────────────────

def _close(conn, row, price: float, reason: str, icon: str):
    pct = (price / row["entry_price"] - 1) * 100
    pnl = (row["stake_sol"] or 0) * pct / 100
    conn.execute(
        """UPDATE paper_trades SET status='cerrada', exit_price=?,
           exit_ts=?, exit_reason=?, pnl_pct=?, pnl_sol=? WHERE id=?""",
        (price, int(time.time()), reason, pct, pnl, row["id"]))
    conn.commit()
    res = "🟢" if pnl >= 0 else "🔴"
    _tg(f"{icon} *Paper cerrada* ({reason}): *{row['symbol']}*\n"
        f"${row['entry_price']:.6g} → ${price:.6g}  ({pct:+.0f}%)\n"
        f"{res} PnL: *{pnl:+.3f} SOL* sobre {row['stake_sol']:.2f} SOL\n"
        f"Resumen: /paper")
    print(f"🧪 Paper cerrada {row['symbol']} por {reason}: {pnl:+.3f} SOL")


def close_on_wallet_sell(conn, trade: dict, token: dict):
    """La billetera que originó la señal vendió → cerramos con ella."""
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE mint=? AND status='abierta'",
        (trade["mint"],)).fetchone()
    if not row:
        return
    price = token.get("price")
    if not price or price <= 0:
        return
    _close(conn, row, price, "venta de la ⭐", "🚪")


def update_open_trades() -> int:
    """Job periódico: revisa TP / SL / tiempo de las posiciones abiertas.
    Devuelve cuántas cerró."""
    conn = get_conn()
    if not _enabled(conn):
        conn.close()
        return 0
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='abierta' "
        "ORDER BY entry_ts").fetchall()
    if not rows:
        conn.close()
        return 0
    tp = _f(conn, "paper_tp_pct", 100.0)
    sl = -abs(_f(conn, "paper_sl_pct", 50.0))
    timeout = _f(conn, "paper_timeout_h", 48.0) * HOUR
    now = time.time()

    import config
    from signal_tracker import _price
    cerradas = 0
    for row in rows:
        price = _price(row["mint"])
        time.sleep(config.DEXSCREENER_DELAY)
        if not price:
            # Sin precio (¿par muerto?): si además venció el tiempo,
            # cerramos como pérdida total para no dejarla zombi.
            if now - row["entry_ts"] > timeout:
                _close(conn, row, row["entry_price"] * 0.01,
                       "sin liquidez", "💀")
                cerradas += 1
            continue
        pct = (price / row["entry_price"] - 1) * 100
        if pct >= tp:
            _close(conn, row, price, "take-profit", "🎯")
            cerradas += 1
        elif pct <= sl:
            _close(conn, row, price, "stop-loss", "🛑")
            cerradas += 1
        elif now - row["entry_ts"] > timeout:
            _close(conn, row, price, "tiempo", "⏰")
            cerradas += 1
    conn.close()
    return cerradas


# ───────────────────────── Resumen (/paper) ───────────────────────────────

def resumen_text() -> str:
    conn = get_conn()
    tp = _f(conn, "paper_tp_pct", 100.0)
    sl = abs(_f(conn, "paper_sl_pct", 50.0))
    timeout = _f(conn, "paper_timeout_h", 48.0)
    max_sol = _f(conn, "paper_max_sol", 1.0)
    estado = "🟢 activo" if _enabled(conn) else "🔴 apagado"

    abiertas = conn.execute(
        "SELECT * FROM paper_trades WHERE status='abierta' "
        "ORDER BY entry_ts DESC").fetchall()
    cer = conn.execute(
        "SELECT COUNT(*) n, SUM(pnl_sol) pnl, "
        "SUM(CASE WHEN pnl_sol>0 THEN 1 ELSE 0 END) wins "
        "FROM paper_trades WHERE status='cerrada'").fetchone()
    por_motivo = conn.execute(
        "SELECT exit_reason r, COUNT(*) n, SUM(pnl_sol) pnl "
        "FROM paper_trades WHERE status='cerrada' "
        "GROUP BY exit_reason ORDER BY n DESC").fetchall()
    conn.close()

    out = [f"🧪 *Paper trading*  ·  {estado}",
           f"Config: tope {max_sol:g} SOL/señal · TP +{tp:.0f}% · "
           f"SL -{sl:.0f}% · máx {timeout:g}h",
           ""]
    n_c = cer["n"] or 0
    if n_c:
        pnl_t = cer["pnl"] or 0.0
        wr = 100.0 * (cer["wins"] or 0) / n_c
        res = "🟢" if pnl_t >= 0 else "🔴"
        out.append(f"{res} *Cerradas:* {n_c} · win rate {wr:.0f}% · "
                   f"PnL total *{pnl_t:+.2f} SOL*")
        for m in por_motivo:
            out.append(f"   · {m['r']}: {m['n']}  ({(m['pnl'] or 0):+.2f} SOL)")
    else:
        out.append("Aún no hay operaciones cerradas.")
    out.append("")
    if abiertas:
        out.append(f"📂 *Abiertas ({len(abiertas)}):*")
        now = time.time()
        for r in abiertas[:15]:
            hs = (now - r["entry_ts"]) / HOUR
            out.append(f"   · *{r['symbol']}* {r['stake_sol']:.2f} SOL "
                       f"@ ${r['entry_price']:.6g} · hace {hs:.1f}h")
    else:
        out.append("📂 Sin posiciones abiertas.")
    out.append("\nComandos: /paper on · /paper off · /paper max <SOL>")
    return "\n".join(out)
