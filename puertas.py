"""
Las tres puertas: quien merece que le copiemos.
(Embudo v2, fase 9 — 09/09/2026)

POR QUE EXISTE
--------------
Las fases anteriores MIDEN. Esta DECIDE — o mejor dicho, prepara la
decision: pone a cada billetera en una etapa (`wallets.wallet_stage`) y
deja escrito por que, para que el dueño pueda discutirlo con
`/porque <billetera>` en vez de tragarse un numero.

Se llaman puertas y no "puntuacion" a proposito: son tres condiciones que
hay que pasar EN ORDEN, y basta fallar una para quedarse fuera. Una nota
media alta puede esconder un cero en algo que importa; una puerta no.

LAS TRES, EN LAS PALABRAS DEL DUEÑO
------------------------------------
1. **¿Se le puede copiar?** No es un bot, y no entra y sale tan rapido
   que cuando nuestra alerta llegue ya se haya ido. Da igual lo bien que
   opere: si no se le puede seguir, no sirve.
2. **¿Gana de verdad?** Con material suficiente para saberlo (no una
   operacion con suerte), multiplo tipico por encima de 1 y mas aciertos
   que fallos.
3. **¿Es la estrategia que quiero?** Aguanta mas de un dia y lo hace con
   POCO capital. Esta es la puerta que separa lo que el dueño busca de lo
   que el embudo viejo encontraba.

ETAPAS
------
- `copiable`     — pasa las tres.
- `observacion`  — pasa 1 y 2, falla la 3 (gana, pero no como el dueño
                   quiere: voltea rapido o mueve demasiado dinero).
- `candidata`    — pasa la 1, falla la 2 (se le podria copiar, pero
                   todavia no esta demostrado que gane).
- `descartada`   — falla la 1.
- `sin_datos`    — no hay con que juzgarla; NO es un suspenso.

LO QUE ESTO **NO** HACE (regla 55 del encargo)
----------------------------------------------
No mueve dinero, no abre ni cierra nada, y **no cambia a quien se copia**.
Escribe `wallet_stage` y lo enseña. Que la etapa mande sobre las alertas y
el paper trading depende de `EMBUDO_V2_ACTIVO`, que sigue en 0 hasta que
el dueño diga lo contrario; hay una prueba dedicada a que con el
interruptor apagado nada cambia de sitio.

DE DONDE SALEN LOS UMBRALES
---------------------------
De lo medido en la base, no de mi cabeza — y cada uno se puede mover por
configuracion:
- El capital tipico se compara con la banda de la fase 8 (0,5-5 SOL), que
  salio de las 2.555 posiciones medidas: por debajo de medio SOL solo el
  37 % acaba en ganancia, y por encima de 30 el multiplo tipico se cae a
  x2,6.
- "Aguantar" es HOLD_MIN_HOURS (24 h), el criterio que el dueño puso por
  escrito.
- El minimo de posiciones medidas es el mismo de la fase 6 (3): con una
  sola operacion no se distingue el acierto de la suerte.
"""

import config
from db import get_conn
from avisos import aviso as _avisar_ex

HORA = 3600

ETAPAS = ("copiable", "observacion", "candidata", "descartada", "sin_datos")


def _f(nombre, defecto):
    try:
        return float(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return float(defecto)


def _i(nombre, defecto):
    try:
        return int(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return int(defecto)


# ── las tres puertas ─────────────────────────────────────────────────

def puerta1(d: dict) -> tuple:
    """¿Se le puede copiar? (bool, motivo)"""
    if d.get("is_bot"):
        return (False, "marcada como bot: no es una persona operando")
    h = d.get("hold_median_h")
    minimo = _f("PUERTA_MIN_HOLD_COPIABLE_H", 1.0)
    if h is None:
        return (False, "no se sabe cuánto aguanta")
    if h < minimo:
        return (False, f"entra y sale en {h:.1f} h de media: cuando la "
                       f"alerta llegue ya se habrá ido")
    return (True, f"aguanta {h:.1f} h de media, da tiempo a seguirla")


def puerta2(d: dict) -> tuple:
    """¿Gana de verdad? (bool, motivo)"""
    n = int(d.get("posiciones") or 0)
    minimo = _i("CALIDAD_MIN_POSICIONES", 3)
    if n < minimo:
        return (False, f"solo {n} operaciones medidas: hacen falta "
                       f"{minimo} para distinguir el acierto de la suerte")
    mult = d.get("mult_realizado")
    if mult is None:
        return (False, "sin múltiplo medido")
    if mult <= _f("PUERTA_MIN_MULTIPLO", 1.0):
        return (False, f"su operación típica es x{mult:.2f}: no gana")
    cons = d.get("q_consistency")
    minc = _f("PUERTA_MIN_CONSISTENCIA", 50.0)
    if cons is None:
        return (False, "sin medida de cuántas veces acierta")
    if cons < minc:
        return (False, f"acierta el {cons:.0f} % de las veces, menos de "
                       f"{minc:.0f} %")
    return (True, f"x{mult:.2f} típico acertando el {cons:.0f} % en "
                  f"{n} operaciones")


def puerta3(d: dict) -> tuple:
    """¿Es la estrategia que el dueño quiere? (bool, motivo)"""
    h = d.get("hold_median_h")
    hold_h = _f("HOLD_MIN_HOURS", 24.0)
    if h is None or h < hold_h:
        if h is None:
            return (False, "no se sabe cuánto aguanta")
        return (False, f"aguanta {h:.1f} h de media, menos del día que "
                       f"pides")
    cap = d.get("capital_tipico")
    lo = _f("DESCUBRIMIENTO_MIN_SOL", 0.5)
    hi = _f("DESCUBRIMIENTO_MAX_SOL", 5.0)
    if cap is None:
        return (False, "no se sabe con cuánto capital opera")
    if cap > hi:
        return (False, f"mueve {cap:.1f} SOL por operación: gana a menudo "
                       f"pero multiplica poco")
    if cap < lo:
        return (False, f"mueve {cap:.2f} SOL por operación: con tan poco, "
                       f"el múltiplo no significa nada")
    return (True, f"aguanta {h:.0f} h con {cap:.1f} SOL típicos")


def evaluar(d: dict) -> dict:
    """Las tres puertas sobre una billetera. Devuelve etapa y motivos."""
    if not d or d.get("posiciones") is None:
        return {"wallet_stage": "sin_datos", "p1": None, "p2": None,
                "p3": None, "motivo": "todavía no hay nada medido",
                "motivos": []}
    ok1, m1 = puerta1(d)
    ok2, m2 = puerta2(d)
    ok3, m3 = puerta3(d)
    if not ok1:
        etapa, motivo = "descartada", m1
    elif not ok2:
        etapa, motivo = "candidata", m2
    elif not ok3:
        etapa, motivo = "observacion", m3
    else:
        etapa, motivo = "copiable", m3
    return {"wallet_stage": etapa, "p1": ok1, "p2": ok2, "p3": ok3,
            "motivo": motivo, "motivos": [m1, m2, m3]}


# ── datos de una billetera ───────────────────────────────────────────

def datos(conn, wallet: str) -> dict | None:
    """Lo que hace falta para las tres puertas, en una consulta."""
    try:
        f = conn.execute(
            """SELECT w.address, COALESCE(w.is_bot, 0) is_bot,
                      w.q_score, w.q_consistency, w.hold_median_h,
                      w.mult_realizado, w.estrategia, w.wallet_stage,
                      COALESCE(w.is_tracked, 0) is_tracked
               FROM wallets w WHERE w.address = ?""", (wallet,)).fetchone()
    except Exception as _ex:
        _avisar_ex("puertas:datos", _ex)
        return None
    d = dict(f) if f else {"address": wallet, "is_bot": 0, "q_score": None,
                           "q_consistency": None, "hold_median_h": None,
                           "mult_realizado": None, "estrategia": None,
                           "wallet_stage": None, "is_tracked": 0}
    try:
        r = conn.execute(
            """SELECT COUNT(*) n, AVG(sol_in) cap FROM wallet_positions
               WHERE wallet = ? AND history_complete = 1
                 AND position_status = 'cerrada'""", (wallet,)).fetchone()
        d["posiciones"] = int(r["n"] or 0)
        d["capital_tipico"] = r["cap"]
    except Exception as _ex:
        _avisar_ex("puertas:datos:posiciones", _ex)
        d["posiciones"] = 0
        d["capital_tipico"] = None
    return d


def guardar(conn, wallet: str, veredicto: dict) -> bool:
    """Escribe la etapa. Crea la ficha si falta (las cazadas llegan por su
    historial, no por una aparicion) y dice False si no escribio nada —
    la leccion de la 19-BI."""
    if not wallet or not veredicto:
        return False
    try:
        from db import now_iso
        conn.execute(
            """INSERT OR IGNORE INTO wallets (address, first_seen,
                                              last_updated)
               VALUES (?,?,?)""", (wallet, now_iso(), now_iso()))
    except Exception as _ex:
        _avisar_ex("puertas:guardar:alta", _ex)
    try:
        cur = conn.execute(
            "UPDATE wallets SET wallet_stage = ? WHERE address = ?",
            (veredicto.get("wallet_stage"), wallet))
        return bool(cur.rowcount)
    except Exception as _ex:
        _avisar_ex("puertas:guardar", _ex)
        return False


# ── la pasada ────────────────────────────────────────────────────────

def _a_evaluar(conn, limite: int) -> list[str]:
    """Billeteras con nota, empezando por las que no tienen etapa."""
    try:
        filas = conn.execute(
            """SELECT address FROM wallets WHERE q_score IS NOT NULL
               ORDER BY CASE WHEN wallet_stage IS NULL THEN 0 ELSE 1 END,
                        q_ts DESC LIMIT ?""", (int(limite),)).fetchall()
        return [f["address"] for f in filas]
    except Exception as _ex:
        _avisar_ex("puertas:_a_evaluar", _ex)
        return []


def revisar(limite: int | None = None) -> dict:
    """Pasada periodica. Cero creditos: todo sale de lo ya medido."""
    if limite is None:
        limite = _i("PUERTAS_POR_PASADA", 300)
    conn = get_conn()
    try:
        cuenta = {e: 0 for e in ETAPAS}
        cambios = []
        for w in _a_evaluar(conn, limite):
            d = datos(conn, w)
            v = evaluar(d)
            antes = (d or {}).get("wallet_stage")
            if guardar(conn, w, v):
                cuenta[v["wallet_stage"]] = cuenta.get(
                    v["wallet_stage"], 0) + 1
                if antes != v["wallet_stage"]:
                    cambios.append((w, antes, v["wallet_stage"],
                                    v["motivo"]))
        conn.commit()
        try:
            from analysis_events import registrar_lote, registrar
            registrar_lote(conn, [
                {"entity_type": "wallet", "entity_id": w,
                 "stage": "puertas", "decision": nueva,
                 "reason": f"{antes or 'sin etapa'} → {nueva}: {mot}"}
                for w, antes, nueva, mot in cambios[:200]])
            registrar(conn, "lote", "puertas", "puertas", "revisadas",
                      " · ".join(f"{k} {v}" for k, v in cuenta.items() if v),
                      score=sum(cuenta.values()),
                      data_source="wallet_positions+wallets")
        except Exception as _ex:
            _avisar_ex("puertas:revisar:eventos", _ex)
        print("✓ Puertas: " + " · ".join(f"{k} {v}" for k, v in
                                         cuenta.items() if v)
              + f" ({len(cambios)} cambios) · 0 créditos")
        return cuenta
    finally:
        conn.close()


# ── lo que decide (o no) ─────────────────────────────────────────────

def manda() -> bool:
    """¿La etapa decide ya a quien se copia?

    Hoy NO: `EMBUDO_V2_ACTIVO` esta en 0. Existe para que el dia que el
    dueño lo encienda no haya que tocar codigo, y para que quede UN solo
    sitio donde mirarlo.
    """
    return bool(_i("EMBUDO_V2_ACTIVO", 0))


def copiables(conn, limite: int = 50) -> list[dict]:
    try:
        return [dict(f) for f in conn.execute(
            """SELECT address, q_score, mult_realizado, hold_median_h,
                      pos_24h, estrategia, COALESCE(is_tracked,0) is_tracked
               FROM wallets WHERE wallet_stage = 'copiable'
               ORDER BY q_score DESC LIMIT ?""", (int(limite),)).fetchall()]
    except Exception as _ex:
        _avisar_ex("puertas:copiables", _ex)
        return []


# ── textos ───────────────────────────────────────────────────────────

def porque_text(conn, wallet: str) -> str:
    """`/porque <billetera>`: las tres puertas, una por una."""
    d = datos(conn, wallet)
    if d is None:
        return "No pude leer esa billetera."
    v = evaluar(d)
    if v["wallet_stage"] == "sin_datos":
        return (f"`{wallet[:12]}…` todavía no tiene nada medido. No es un "
                f"suspenso: es que aún no hay con qué juzgarla.")
    marca = {True: "✅", False: "❌", None: "·"}
    L = [f"🚦 `{wallet[:14]}…` → *{v['wallet_stage']}*", ""]
    for n, (etq, ok, mot) in enumerate((
            ("¿Se le puede copiar?", v["p1"], v["motivos"][0]),
            ("¿Gana de verdad?", v["p2"], v["motivos"][1]),
            ("¿Es tu estrategia?", v["p3"], v["motivos"][2])), 1):
        L.append(f"{marca.get(ok)} *Puerta {n} · {etq}*")
        L.append(f"    _{mot}_")
    L.append("")
    cap = d.get("capital_tipico")
    L.append(f"Operación típica: x{(d.get('mult_realizado') or 0):.2f} · "
             f"{(d.get('hold_median_h') or 0):.0f} h dentro · "
             f"{cap:.2f} SOL" if cap is not None else "")
    if not manda():
        L.append("")
        L.append("_Las puertas todavía no deciden a quién se copia: eso "
                 "sigue mandándolo el top de siempre._")
    return "\n".join(x for x in L if x is not None)


def ranking_text(conn, limite: int = 15) -> str:
    """`/ranking`: las que pasan las tres puertas."""
    try:
        cuenta = conn.execute(
            "SELECT wallet_stage e, COUNT(*) n FROM wallets "
            "WHERE wallet_stage IS NOT NULL GROUP BY e ORDER BY n DESC"
        ).fetchall()
    except Exception as _ex:
        _avisar_ex("puertas:ranking_text", _ex)
        return "🚦 *Ranking*\n\nTodavía no se han evaluado las puertas."
    if not cuenta:
        return ("🚦 *Ranking*\n\nTodavía no se han evaluado las puertas. "
                "Se llenan solas con lo ya medido, sin gastar créditos.")
    L = ["🚦 *Las tres puertas*", ""]
    L.append(" · ".join(f"{f['e']} {f['n']}" for f in cuenta))
    filas = copiables(conn, limite)
    if filas:
        L.append("")
        L.append("*Pasan las tres* (ordenadas por nota)")
        for f in filas:
            est = " ⭐" if f["is_tracked"] else ""
            L.append(f"`{f['address'][:10]}…`{est} nota "
                     f"{(f['q_score'] or 0):.0f} · "
                     f"x{(f['mult_realizado'] or 0):.1f} en "
                     f"{(f['hold_median_h'] or 0):.0f} h · "
                     f"{f['pos_24h'] or 0} aguantes")
    else:
        L.append("")
        L.append("_Ninguna pasa las tres todavía. `/porque <billetera>` "
                 "dice en cuál se cae cada una._")
    if not manda():
        L.append("")
        L.append("_Esto todavía no cambia a quién se copia._")
    return "\n".join(L)


if __name__ == "__main__":
    revisar()
