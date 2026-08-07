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
  - PnL nocional: pnl = monto × variación % del precio del token.
    El monto se guarda en SOL *y* en dólares al cambio del momento de
    entrar, y el PnL se publica en dólares, que es la unidad en la que
    está medida la variación del precio. El PnL en SOL se sigue
    guardando (pnl_sol) para no invalidar el histórico ya cerrado, pero
    ojo: asume que el precio de SOL no se movió, así que es una
    aproximación, no una cifra exacta.
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


# ── Formato de importes ───────────────────────────────────────────────────
# Los tres se importan dentro de la función a propósito: card_image tira de
# Pillow y unrealized_pnl de requests, y no queremos que un fallo de esos
# módulos impida abrir una posición.

def _precio(x) -> str:
    """Precio en dólares legible. $0.00008342, no $8.342e-05."""
    try:
        from card_image import _fmt_price
        return _fmt_price(x)
    except Exception:
        try:
            return f"{float(x):.10f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "?"


def _sol_a_usd() -> float | None:
    """Precio SOL/USD ahora mismo (cache de 5 min en unrealized_pnl)."""
    try:
        from unrealized_pnl import _sol_usd
        return _sol_usd()
    except Exception:
        return None


def _usd(x) -> str:
    """Importe en dólares con separador de miles."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:,.4f}"


def _usd_firmado(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    return ("+" if x >= 0 else "-") + _usd(abs(x))


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
    # Importe en dólares al cambio de AHORA. Se guarda, no se recalcula
    # al cerrar: lo que quieres saber es cuánto dinero habrías puesto.
    su = _sol_a_usd()
    stake_usd = stake * su if su and su > 0 else None
    conn.execute(
        """INSERT INTO paper_trades
           (signature, wallet, mint, symbol, stake_sol, stake_usd,
            entry_price, entry_ts, signal_score, status)
           VALUES (?,?,?,?,?,?,?,?,?, 'abierta')""",
        (trade["signature"], trade["wallet"], trade["mint"], sym,
         stake, stake_usd, price, trade["ts"], score))
    conn.commit()
    monto = (f"{_usd(stake_usd)} ({stake:.2f} SOL)" if stake_usd is not None
             else f"{stake:.2f} SOL")
    print(f"🧪 Paper: compra simulada {monto} en {sym} "
          f"@ ${_precio(price)}")
    _tg(f"🧪 *Paper:* compra simulada\n"
        f"💵 Monto: *{monto}*\n"
        f"🪙 Token: *{sym}*  ·  entrada ${_precio(price)}\n"
        f"📂 {n + 1}/{max_abiertas} abiertas\nVer: /paper")
    return True


# ───────────────────────── Cierres ────────────────────────────────────────

def _campo(row, nombre):
    """row[nombre] tolerante: las filas viejas no tienen las columnas
    nuevas, y sqlite3.Row lanza IndexError en vez de devolver None."""
    try:
        return row[nombre]
    except (IndexError, KeyError, TypeError):
        return None


def _close(conn, row, price: float, reason: str, icon: str):
    pct = (price / row["entry_price"] - 1) * 100
    pnl = (row["stake_sol"] or 0) * pct / 100      # notional en SOL (histórico)

    # PnL en dólares sobre el importe que se guardó al entrar. Si la fila
    # es vieja y no lo tiene, se reconstruye al cambio de ahora; es una
    # aproximación, pero mejor que no dar la cifra.
    stake_usd = _campo(row, "stake_usd")
    if stake_usd is None:
        su = _sol_a_usd()
        stake_usd = (row["stake_sol"] or 0) * su if su and su > 0 else None
    pnl_usd = stake_usd * pct / 100 if stake_usd is not None else None

    # stake_usd se guarda también cuando se ha reconstruido. Si no, el
    # SUM(stake_usd) del resumen se saltaría esa fila y el ROI saldría
    # calculado sobre un total demasiado pequeño — lo detectó la prueba:
    # dos operaciones cerradas y solo una contada en «invertido».
    conn.execute(
        """UPDATE paper_trades SET status='cerrada', exit_price=?,
           exit_ts=?, exit_reason=?, pnl_pct=?, pnl_sol=?, pnl_usd=?,
           stake_usd=? WHERE id=?""",
        (price, int(time.time()), reason, pct, pnl, pnl_usd,
         stake_usd, row["id"]))
    conn.commit()

    res = "🟢" if pnl >= 0 else "🔴"
    if pnl_usd is not None:
        linea_pnl = (f"{res} PnL: *{_usd_firmado(pnl_usd)}*  "
                     f"sobre {_usd(stake_usd)} invertidos")
    else:
        linea_pnl = (f"{res} PnL: *{pnl:+.3f} SOL*  "
                     f"sobre {row['stake_sol']:.2f} SOL")
    _tg(f"{icon} *Paper cerrada* ({reason}): *{row['symbol']}*\n"
        f"💵 Precio: ${_precio(row['entry_price'])} → "
        f"*${_precio(price)}*  ({pct:+.0f}%)\n"
        f"{linea_pnl}\n"
        f"Resumen: /paper")
    print(f"🧪 Paper cerrada {row['symbol']} por {reason}: "
          f"{_usd_firmado(pnl_usd) if pnl_usd is not None else f'{pnl:+.3f} SOL'}")


def close_on_wallet_sell(conn, trade: dict, token: dict,
                         pos: dict | None = None, sigue_estrella: bool = True):
    """La billetera que origino la señal vendio → cerramos con ella.
    Solo si es LA MISMA wallet que abrio la señal y la venta es
    significativa (≥50% de su posicion o cierre total); antes cualquier
    venta parcial de cualquier ⭐ cerraba la simulada y sesgaba el PnL."""
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE mint=? AND status='abierta'",
        (trade["mint"],)).fetchone()
    if not row:
        return
    if row["wallet"] and trade.get("wallet") \
            and row["wallet"] != trade["wallet"]:
        return
    if pos and pos.get("known") and not pos.get("fully_sold") \
            and (pos.get("pct_sold") or 100) < 50:
        return
    price = token.get("price")
    if not price or price <= 0:
        return
    motivo = ("venta de la ⭐" if sigue_estrella
              else "venta (la billetera ya no es ⭐)")
    _close(conn, row, price, motivo, "🚪")


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
        "SELECT COUNT(*) n, SUM(pnl_sol) pnl, SUM(pnl_usd) pnl_usd, "
        "SUM(stake_usd) invertido, "
        "SUM(CASE WHEN stake_usd IS NULL THEN 1 ELSE 0 END) sin_usd, "
        "SUM(CASE WHEN pnl_sol>0 THEN 1 ELSE 0 END) wins "
        "FROM paper_trades WHERE status='cerrada'").fetchone()
    por_motivo = conn.execute(
        "SELECT exit_reason r, COUNT(*) n, SUM(pnl_sol) pnl, "
        "SUM(pnl_usd) pnl_usd "
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
        pnl_usd_t = cer["pnl_usd"]
        invertido = cer["invertido"]
        wr = 100.0 * (cer["wins"] or 0) / n_c
        res = "🟢" if (pnl_usd_t if pnl_usd_t is not None
                       else pnl_t) >= 0 else "🔴"
        total = (_usd_firmado(pnl_usd_t) if pnl_usd_t is not None
                 else f"{pnl_t:+.2f} SOL")
        out.append(f"{res} *Cerradas:* {n_c} · win rate {wr:.0f}% · "
                   f"PnL total *{total}*")
        if invertido:
            roi = 100.0 * (pnl_usd_t or 0) / invertido
            # Aviso honesto: si alguna cerrada no tiene importe en
            # dólares, el ROI está calculado sobre menos operaciones.
            faltan = cer["sin_usd"] or 0
            nota = f"  _(sin dato en {faltan})_" if faltan else ""
            out.append(f"   Invertido {_usd(invertido)} → "
                       f"ROI *{roi:+.1f}%*{nota}")
        for m in por_motivo:
            cifra = (_usd_firmado(m["pnl_usd"]) if m["pnl_usd"] is not None
                     else f"{(m['pnl'] or 0):+.2f} SOL")
            out.append(f"   · {m['r']}: {m['n']}  ({cifra})")
    else:
        out.append("Aún no hay operaciones cerradas.")
    out.append("")
    if abiertas:
        out.append(f"📂 *Abiertas ({len(abiertas)}):*")
        now = time.time()
        from card_image import _ago
        for r in abiertas[:15]:
            hs = (now - r["entry_ts"]) / HOUR
            su = _campo(r, "stake_usd")
            monto = _usd(su) if su is not None else f"{r['stake_sol']:.2f} SOL"
            out.append(f"   · *{r['symbol']}* {monto} "
                       f"@ ${_precio(r['entry_price'])} · {_ago(hs)}")
    else:
        out.append("📂 Sin posiciones abiertas.")
    out.append("\nComandos: /paper on · /paper off · /paper max <SOL> · "
               "/paper reset")
    return "\n".join(out)


# ───────────────────────── Reinicio ───────────────────────────────────────

def contar() -> tuple[int, int]:
    """(abiertas, cerradas) — para avisar de lo que se va a borrar."""
    conn = get_conn()
    try:
        a = conn.execute("SELECT COUNT(*) c FROM paper_trades "
                         "WHERE status='abierta'").fetchone()["c"]
        c = conn.execute("SELECT COUNT(*) c FROM paper_trades "
                         "WHERE status='cerrada'").fetchone()["c"]
        return a, c
    except Exception:
        return 0, 0
    finally:
        conn.close()


def reset() -> tuple[int, int]:
    """Borra TODO el historial simulado y empieza de cero.

    Devuelve cuántas abiertas y cerradas había. Es irreversible: por eso
    el botón de Telegram pide confirmación antes de llamar aquí.

    También limpia las marcas 'mult_alert:*' de settings, que son los
    múltiplos ya avisados por token. Si no se borran, tras el reinicio un
    token que ya hizo x5 no volvería a avisar hasta x6.
    """
    abiertas, cerradas = contar()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM paper_trades")
        try:
            conn.execute("DELETE FROM settings WHERE key LIKE 'mult_alert:%'")
        except Exception as e:
            print(f"· Reset: no pude limpiar mult_alert: {e}")
        conn.commit()
    finally:
        conn.close()
    print(f"🧪 Paper trading reiniciado: {abiertas} abiertas y "
          f"{cerradas} cerradas borradas")
    return abiertas, cerradas
