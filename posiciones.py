"""
Reconstruccion de posiciones: que hizo cada billetera en cada token.
(Embudo v2, fase 5 — 08/09/2026)

POR QUE EXISTE
--------------
El embudo viejo solo mira COMPRAS. Registra "esta billetera compro pronto"
y con eso decide. Nunca mira la VENTA, asi que no sabe lo unico que el
dueño pide: cuanto GANO de verdad en ese token y cuanto tiempo lo AGUANTO.
Una billetera que compro la primera y vendio a los 40 segundos con perdida
puntuaba igual que una que aguanto tres dias y multiplico por diez.

Aqui se junta compra y venta en una sola fila por (billetera, token):
`wallet_positions`. De ahi salen las tres respuestas que faltaban:
  · cuanto metio y cuanto saco (sol_in / sol_out / realized_pnl),
  · cuanto tiempo estuvo dentro (holding_seconds, held_24h, held_7d),
  · y si eso que calculamos es FIABLE o solo la mitad de la historia.

DE DONDE SALEN LOS DATOS (cero creditos de Helius)
--------------------------------------------------
De `trades`, el historial propio que el perfilador lleva guardando desde
diciembre: 267.585 operaciones de 4.009 billeteras en 29.511 tokens, con
63.349 pares (billetera, token) que tienen compra Y venta. Ese dato ya
esta pagado y hasta hoy solo se usaba para metricas sueltas por billetera,
nunca por token.

HONESTIDAD DEL DATO (regla 54 del encargo)
------------------------------------------
El peligro de esta tabla es inventar un PnL. Tres reglas lo evitan:

1. Si vendio MAS tokens de los que le vimos comprar, la historia esta
   incompleta (compro antes de nuestra ventana, o le llegaron por
   transferencia o airdrop): la posicion se marca `incoherente`,
   `history_complete = 0` y la confianza baja. No se tira la fila —dice
   algo real— pero no puede puntuar como si estuviera medida.
2. En una posicion a medio vender, el coste NO es todo lo que metio: se
   usa la parte proporcional (`sol_in * fraccion_vendida`). Restar el
   total daria perdidas falsas a quien todavia va ganando.
3. Lo NO realizado necesita un precio de hoy. Si no lo hay, queda en NULL
   —"todavia no se sabe"— nunca en cero.
"""

import time

import config
from db import get_conn
from avisos import aviso as _avisar_ex

HORA = 3600
DIA = 86400

# Los umbrales se leen de `config` EN CADA LLAMADA, no al importar: asi el
# dueño puede moverlos con una variable de entorno y reiniciar, y asi las
# pruebas pueden comprobar que de verdad mandan (una constante congelada
# al importar haria pasar la prueba sin que el umbral se use).


def _min_frac() -> float:
    """Fraccion vendida a partir de la cual la posicion cuenta como
    CERRADA. No se exige el 100 %: las comisiones y el redondeo de
    decimales del token dejan polvo dentro."""
    return float(getattr(config, "POSICION_CERRADA_MIN_FRAC", 0.70))


def _max_frac() -> float:
    """Por encima de esto vendio MAS de lo que le vimos comprar."""
    return float(getattr(config, "POSICION_CERRADA_MAX_FRAC", 1.05))


def _hold_h() -> float:
    """Cuantas horas dentro cuentan como "aguanto" (criterio del dueño)."""
    return float(getattr(config, "HOLD_MIN_HOURS", 24.0))


COLUMNAS = ("first_buy_ts", "last_buy_ts", "first_sell_ts", "last_sell_ts",
            "buy_count", "sell_count", "tokens_bought", "tokens_sold",
            "sol_in", "sol_out", "realized_pnl", "remaining_tokens",
            "unrealized_pnl", "net_pnl", "roi_pct", "holding_seconds",
            "held_24h", "held_7d", "position_status", "history_complete",
            "pnl_confidence", "updated_ts")


def _f(v, por_defecto=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return por_defecto


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── el calculo ────────────────────────────────────────────────────────

def calcular(operaciones: list[dict], precio_actual=None,
             ahora: int | None = None) -> dict | None:
    """Una posicion a partir de las operaciones de UNA billetera en UN token.

    `operaciones`: dicts con side ('compra'/'venta'), sol, tokens, ts.
    `precio_actual`: SOL por token, hoy. Si no se sabe, se pasa None y lo
    no realizado queda en NULL (nunca en cero).

    Devuelve None si no hay ni una compra ni una venta utilizable.
    """
    ahora = int(ahora if ahora is not None else time.time())
    min_frac, max_frac, hold_h = _min_frac(), _max_frac(), _hold_h()
    sol_in = sol_out = tok_in = tok_out = 0.0
    nb = ns = 0
    t_prim_c = t_ult_c = t_prim_v = t_ult_v = None
    for o in operaciones or []:
        ts = _i(o.get("ts"))
        sol = _f(o.get("sol"))
        tok = _f(o.get("tokens"))
        lado = (o.get("side") or "").strip().lower()
        if lado in ("compra", "buy"):
            nb += 1
            sol_in += max(0.0, sol)
            tok_in += max(0.0, tok)
            if ts:
                t_prim_c = ts if t_prim_c is None else min(t_prim_c, ts)
                t_ult_c = ts if t_ult_c is None else max(t_ult_c, ts)
        elif lado in ("venta", "sell"):
            ns += 1
            sol_out += max(0.0, sol)
            tok_out += max(0.0, tok)
            if ts:
                t_prim_v = ts if t_prim_v is None else min(t_prim_v, ts)
                t_ult_v = ts if t_ult_v is None else max(t_ult_v, ts)
    if nb == 0 and ns == 0:
        return None

    # ── ¿esta historia esta entera? ──
    # Vender mas de lo comprado significa que la compra no la vimos.
    frac = (tok_out / tok_in) if tok_in > 0 else None
    completo = 1
    if nb == 0:
        estado, completo = "solo_ventas", 0
    elif ns == 0:
        estado = "abierta"
    elif frac is None:
        # Vendio y compro, pero sin cantidades de token no se puede saber
        # cuanto queda dentro.
        estado, completo = "sin_cantidades", 0
    elif frac > max_frac:
        estado, completo = "incoherente", 0
    elif frac >= min_frac:
        estado = "cerrada"
    else:
        estado = "parcial"

    # ── coste proporcional a lo vendido ──
    # (regla 2 del encabezado) En una posicion a medio vender el coste de
    # lo vendido es solo su parte; restar todo lo invertido daria perdidas
    # falsas a quien todavia va ganando con el resto dentro.
    if estado == "cerrada":
        coste_vendido = sol_in
    elif frac and frac > 0 and estado in ("parcial", "incoherente"):
        coste_vendido = sol_in * min(1.0, frac)
    elif ns > 0 and nb > 0:
        coste_vendido = sol_in
    else:
        coste_vendido = 0.0
    realizado = (sol_out - coste_vendido) if ns > 0 else 0.0

    # ── lo que sigue dentro ──
    restante = max(0.0, tok_in - tok_out) if tok_in > 0 else None
    no_realizado = None
    if restante is not None and precio_actual is not None:
        try:
            p = float(precio_actual)
            if p >= 0:
                coste_restante = sol_in - coste_vendido
                no_realizado = restante * p - coste_restante
        except (TypeError, ValueError):
            no_realizado = None
    # NULL no es cero: si no hay precio, el neto solo puede ser el
    # realizado cuando ya no queda nada dentro.
    if no_realizado is not None:
        neto = realizado + no_realizado
    elif estado == "cerrada":
        neto = realizado
    else:
        neto = None
    roi = (realizado / sol_in * 100.0) if (sol_in > 0 and ns > 0) else None

    # ── cuanto tiempo estuvo dentro ──
    # Para una posicion cerrada, de su primera compra a su ultima venta.
    # Para una abierta, hasta HOY: el tiempo que lleva aguantando es real
    # aunque todavia no haya vendido.
    dentro = None
    if t_prim_c:
        fin = t_ult_v if (estado == "cerrada" and t_ult_v) else ahora
        if fin >= t_prim_c:
            dentro = int(fin - t_prim_c)

    # ── confianza del PnL ──
    if not completo:
        conf = "baja"
    elif estado == "cerrada":
        conf = "alta"
    elif estado == "parcial":
        conf = "media"
    else:                      # abierta: el resultado aun no existe
        conf = "media" if no_realizado is not None else "baja"

    return {
        "first_buy_ts": t_prim_c, "last_buy_ts": t_ult_c,
        "first_sell_ts": t_prim_v, "last_sell_ts": t_ult_v,
        "buy_count": nb, "sell_count": ns,
        "tokens_bought": tok_in, "tokens_sold": tok_out,
        "sol_in": sol_in, "sol_out": sol_out,
        "realized_pnl": realizado, "remaining_tokens": restante,
        "unrealized_pnl": no_realizado, "net_pnl": neto, "roi_pct": roi,
        "holding_seconds": dentro,
        "held_24h": (1 if (dentro is not None and dentro >= hold_h * HORA)
                     else 0),
        "held_7d": (1 if (dentro is not None and dentro >= 7 * DIA) else 0),
        "position_status": estado, "history_complete": completo,
        "pnl_confidence": conf, "updated_ts": ahora,
    }


# ── escritura ─────────────────────────────────────────────────────────

def guardar(conn, wallet: str, mint: str, pos: dict) -> bool:
    """Escribe (o reescribe) la posicion. Idempotente por (wallet, mint).

    Se hace con UPDATE y, si no habia fila, INSERT: `INSERT OR REPLACE` es
    de SQLite y `ON CONFLICT` se escribe distinto en cada motor, y este
    modulo tiene que funcionar en los dos.
    """
    if not wallet or not mint or not pos:
        return False
    vals = [pos.get(c) for c in COLUMNAS]
    try:
        sets = ", ".join(f"{c} = ?" for c in COLUMNAS)
        cur = conn.execute(
            f"UPDATE wallet_positions SET {sets} WHERE wallet = ? AND mint = ?",
            (*vals, wallet, mint))
        if not cur.rowcount:
            cols = ", ".join(COLUMNAS)
            hue = ", ".join("?" for _ in COLUMNAS)
            conn.execute(
                f"INSERT INTO wallet_positions (wallet, mint, {cols}) "
                f"VALUES (?, ?, {hue})", (wallet, mint, *vals))
        return True
    except Exception as _ex:
        _avisar_ex("posiciones:guardar", _ex)
        return False


def reconstruir_mint(conn, mint: str, precios: dict | None = None) -> int:
    """Reconstruye TODAS las posiciones de un token desde `trades`.
    Devuelve cuantas filas se escribieron."""
    if not mint:
        return 0
    try:
        filas = conn.execute(
            "SELECT wallet, side, sol, tokens, ts FROM trades "
            "WHERE mint = ? ORDER BY wallet, ts", (mint,)).fetchall()
    except Exception as _ex:
        _avisar_ex("posiciones:reconstruir_mint", _ex)
        return 0
    por_wallet: dict[str, list] = {}
    for f in filas:
        por_wallet.setdefault(f["wallet"], []).append(dict(f))
    precio = (precios or {}).get(mint)
    n = 0
    for w, ops in por_wallet.items():
        pos = calcular(ops, precio_actual=precio)
        if pos and guardar(conn, w, mint, pos):
            n += 1
    try:
        conn.commit()
    except Exception as _ex:
        _avisar_ex("posiciones:reconstruir_mint:commit", _ex)
    return n


def _mints_objetivo(conn, limite: int) -> list[str]:
    """Los tokens que interesan: los que cruzaron MIN_WINNER_MC y de los
    que tenemos operaciones TODAVIA SIN CONVERTIR en posicion.

    (19-BI) Antes se pedian los tokens SIN NINGUNA posicion, y eso
    convertia la reconstruccion en algo de una sola vez: en cuanto un
    token tenia una posicion, las operaciones que llegaran despues —las
    de cada billetera nueva que el perfilador o la caceria trajeran— no
    se convertian JAMAS. Medido en la base del dueño el 09/09: 362 de los
    369 tokens con hito tenian trabajo pendiente, y de las 75 billeteras
    cazadas solo 154 de sus 324 pares (billetera, token) tenian posicion.
    Por eso solo UNA de las 75 llego a tener nota.

    Ahora la condicion mira el PAR (billetera, token): entra el token que
    tiene al menos una billetera sin reconstruir. Como `reconstruir_mint`
    rehace el token entero, de paso se refrescan las que ya estaban.
    """
    nivel = float(getattr(config, "MIN_WINNER_MC", 500_000))
    try:
        filas = conn.execute(
            """SELECT t.mint, COUNT(*) n FROM trades t
               WHERE EXISTS (SELECT 1 FROM token_milestones m
                             WHERE m.mint = t.mint AND m.milestone_usd = ?)
                 AND NOT EXISTS (SELECT 1 FROM wallet_positions p
                                 WHERE p.mint = t.mint
                                   AND p.wallet = t.wallet)
               GROUP BY t.mint ORDER BY n DESC LIMIT ?""",
            (nivel, int(limite))).fetchall()
        return [f["mint"] for f in filas]
    except Exception as _ex:
        _avisar_ex("posiciones:_mints_objetivo", _ex)
        return []


def reconstruir(limite: int | None = None) -> int:
    """Pasada periodica: reconstruye las posiciones de los tokens
    ganadores que todavia no las tienen. Cero creditos de Helius."""
    if limite is None:
        limite = int(getattr(config, "POSICIONES_TOKENS_POR_PASADA", 20))
    conn = get_conn()
    try:
        mints = _mints_objetivo(conn, limite)
        if not mints:
            print("· Posiciones: nada nuevo que reconstruir")
            return 0
        total = 0
        for mint in mints:
            n = reconstruir_mint(conn, mint)
            total += n
            try:
                from analysis_events import registrar
                registrar(conn, "token", mint, "posiciones", "reconstruido",
                          f"{n} posiciones desde el historial propio",
                          score=n, data_source="trades")
            except Exception as _ex:
                _avisar_ex("posiciones:reconstruir:evento", _ex)
        print(f"✓ Posiciones: {total} filas en {len(mints)} tokens "
              f"(0 creditos de Helius)")
        return total
    finally:
        conn.close()


# ── lectura ───────────────────────────────────────────────────────────

def mejores_del_token(conn, mint: str, limite: int = 20) -> list[dict]:
    """Las billeteras que MAS ganaron en ese token, temprano o tarde.
    Es literalmente lo que pidio el dueño. Solo con PnL medido: las
    posiciones de historia incompleta no compiten."""
    try:
        filas = conn.execute(
            """SELECT wallet, sol_in, sol_out, realized_pnl, roi_pct,
                      holding_seconds, held_24h, position_status,
                      pnl_confidence
               FROM wallet_positions
               WHERE mint = ? AND history_complete = 1 AND sell_count > 0
               ORDER BY realized_pnl DESC LIMIT ?""",
            (mint, int(limite))).fetchall()
        return [dict(f) for f in filas]
    except Exception as _ex:
        _avisar_ex("posiciones:mejores_del_token", _ex)
        return []


def _horas(seg):
    return (seg / HORA) if seg else 0.0


def resumen_text(conn=None, limite: int = 8) -> str:
    """Texto para /posiciones."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        try:
            tot = conn.execute(
                "SELECT COUNT(*) c FROM wallet_positions").fetchone()["c"]
        except Exception as _ex:
            _avisar_ex("posiciones:resumen_text:total", _ex)
            return "📊 *Posiciones*\n\nTodavía no hay posiciones reconstruidas."
        if not tot:
            return ("📊 *Posiciones*\n\nTodavía no hay ninguna reconstruida. "
                    "Se llenan solas con el historial que ya tenemos "
                    "guardado, sin gastar créditos.")
        L = [f"📊 *Posiciones reconstruidas* — {tot:,}".replace(",", ".")]
        filas = conn.execute(
            "SELECT position_status s, COUNT(*) n FROM wallet_positions "
            "GROUP BY s ORDER BY n DESC").fetchall()
        L.append("")
        L.append("*Estado:* " + " · ".join(
            f"{f['s'] or '?'} {f['n']}" for f in filas))
        r = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN held_24h=1 THEN 1 ELSE 0 END) h24,
                      AVG(holding_seconds) med
               FROM wallet_positions
               WHERE position_status='cerrada' AND history_complete=1"""
        ).fetchone()
        if r and r["n"]:
            pc = 100.0 * (r["h24"] or 0) / r["n"]
            L.append(f"*Cerradas medidas:* {r['n']} · aguantaron ≥"
                     f"{_hold_h():g} h: {r['h24'] or 0} ({pc:.0f} %) · "
                     f"media dentro {_horas(r['med']):.1f} h")
        g = conn.execute(
            """SELECT COUNT(*) n, SUM(realized_pnl) p FROM wallet_positions
               WHERE position_status='cerrada' AND history_complete=1
                 AND held_24h=1""").fetchone()
        c = conn.execute(
            """SELECT COUNT(*) n, SUM(realized_pnl) p FROM wallet_positions
               WHERE position_status='cerrada' AND history_complete=1
                 AND held_24h=0""").fetchone()
        if g and c and (g["n"] or c["n"]):
            L.append("")
            L.append("*Aguantar vs. voltear* (PnL realizado)")
            L.append(f"  · ≥{_hold_h():g} h: {g['n'] or 0} posiciones, "
                     f"{(g['p'] or 0):+.1f} SOL")
            L.append(f"  · menos:  {c['n'] or 0} posiciones, "
                     f"{(c['p'] or 0):+.1f} SOL")
        top = conn.execute(
            """SELECT wallet, mint, realized_pnl, roi_pct, holding_seconds
               FROM wallet_positions
               WHERE history_complete=1 AND sell_count>0
               ORDER BY realized_pnl DESC LIMIT ?""",
            (int(limite),)).fetchall()
        if top:
            L.append("")
            L.append("*Mejores resultados medidos*")
            for f in top:
                roi = f"{f['roi_pct']:+.0f} %" if f["roi_pct"] is not None else "?"
                L.append(f"  `{f['wallet'][:8]}…` {f['mint'][:6]}… "
                         f"{(f['realized_pnl'] or 0):+.1f} SOL ({roi}) · "
                         f"{_horas(f['holding_seconds']):.0f} h dentro")
        return "\n".join(L)
    finally:
        if propia:
            conn.close()


if __name__ == "__main__":
    reconstruir()
