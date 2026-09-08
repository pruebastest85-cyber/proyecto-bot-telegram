"""
Nota de calidad de una billetera: ¿merece la pena copiarla?
(Embudo v2, fase 6 — 08/09/2026)

POR QUE EXISTE
--------------
La fase 5 dejo escrito QUE hizo cada billetera en cada token
(`wallet_positions`). Aqui se junta todo lo que hizo una billetera y se
responde a la pregunta del dueño con un numero: **¿esta gente aguanta y
gana, o solo voltea rapido?**

La nota vieja (`wallets.score`) mide otra cosa: cuantas veces aparecio
comprando pronto en tokens que subieron. Premia llegar el primero. Por
eso el top se llenó de francotiradores que compran el minuto cero, sueltan
a los cuarenta segundos y no se pueden copiar. Esa nota **no se toca**:
esta vive aparte, en las columnas `q_*`, y hasta que el dueño diga lo
contrario no decide nada — solo se mide y se enseña.

LAS CINCO PATAS DE LA NOTA (todas 0-100)
----------------------------------------
- **q_hold** — que parte de sus posiciones aguantó mas de HOLD_MIN_HOURS.
  Es el criterio explicito del dueño.
- **q_consistency** — que parte acabó en ganancia. Acertar a menudo vale
  mas que acertar una vez enorme: lo segundo puede ser suerte.
- **q_profit** — cuanto multiplica su operacion TIPICA (la mediana, no la
  media: un solo x400 no puede disfrazar cien operaciones planas).
- **q_survival** — que parte de los tokens en los que entró siguen vivos.
  Entrar en cosas que mueren es una forma de perder aunque se venda a
  tiempo.
- **q_risk** — al reves que las otras: cuanto MENOS se hunde cuando falla.
  100 es "sus perdidas son pequeñas".

`q_score` es la media ponderada de las cinco, con pesos configurables.

HONESTIDAD DEL DATO (regla 54 del encargo)
------------------------------------------
1. Solo entran posiciones **cerradas y con historia completa**: las de
   "vendio mas de lo que le vimos comprar" no puntuan (fase 5).
2. Hace falta un minimo de posiciones (`CALIDAD_MIN_POSICIONES`). Con una
   sola operacion no se sabe si es bueno o tuvo suerte, y la confianza lo
   dice: `pnl_confidence` en la billetera es alta, media o baja segun
   cuanto material haya.
3. **Si una pata no se puede medir, no cuenta como cero: se aparta.**
   La supervivencia de un token puede ser "todavia no se sabe" (fase 4);
   en ese caso `q_survival` queda en NULL y su peso se reparte entre las
   demas, en vez de hundir la nota de alguien por un dato que no tenemos.
"""

import time

import config
from db import get_conn
from avisos import aviso as _avisar_ex

HORA = 3600

# Nombre de las patas y de donde sale su peso en `config`.
PATAS = (("q_hold", "Q_PESO_HOLD"),
         ("q_consistency", "Q_PESO_CONSISTENCIA"),
         ("q_profit", "Q_PESO_PROFIT"),
         ("q_survival", "Q_PESO_SUPERVIVENCIA"),
         ("q_risk", "Q_PESO_RIESGO"))

COLUMNAS = ("q_score", "q_consistency", "q_profit", "q_survival", "q_hold",
            "q_risk", "q_strategy", "q_ts", "estrategia", "hold_median_h",
            "mult_realizado", "tok_500k", "tok_1m", "tok_surv", "pos_24h",
            "pnl_confidence")


def _cfg(nombre, defecto):
    try:
        return float(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return float(defecto)


def _mediana(valores):
    v = sorted(x for x in valores if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def _acotar(x, minimo=0.0, maximo=100.0):
    return max(minimo, min(maximo, x))


# ── las cinco patas ───────────────────────────────────────────────────

def _pata_profit(mult_mediano):
    """De multiplo tipico a nota. x1 (ni gana ni pierde) = 0; x2 = 50;
    x5 o mas = 100. Lineal por tramos, sin premiar el x400 suelto: eso
    ya lo excluye usar la MEDIANA y no la media."""
    if mult_mediano is None:
        return None
    m = float(mult_mediano)
    if m <= 1.0:
        # Perder tambien tiene grados: x0,5 es peor que x0,95.
        return _acotar(max(0.0, (m - 0.5) * 40.0), 0.0, 20.0)
    if m <= 2.0:
        return 20.0 + (m - 1.0) * 30.0
    if m <= 5.0:
        return 50.0 + (m - 2.0) * (50.0 / 3.0)
    return 100.0


def _pata_riesgo(rois):
    """Cuanto MENOS se hunde cuando falla. Se mira la perdida tipica de
    sus operaciones perdedoras: si no pierde nunca, 100."""
    perdidas = [r for r in rois if r is not None and r < 0]
    if not rois:
        return None
    if not perdidas:
        return 100.0
    tipica = _mediana(perdidas)          # negativa
    # -100 % (lo perdio todo) = 0 · -50 % = 50 · 0 % = 100
    return _acotar(100.0 + float(tipica))


def calcular(posiciones: list[dict], hold_h: float | None = None) -> dict | None:
    """La nota de una billetera a partir de SUS posiciones medidas.

    `posiciones`: dicts de `wallet_positions` (ya filtradas a cerradas
    con historia completa) mas, si se sabe, `survived` (1/0/None) del
    token y `hito_1m` / `hito_500k` (1/0).

    Devuelve None si no hay material suficiente.
    """
    if not posiciones:
        return None
    if hold_h is None:
        hold_h = _cfg("HOLD_MIN_HOURS", 24.0)
    minimo = int(_cfg("CALIDAD_MIN_POSICIONES", 3))
    n = len(posiciones)
    if n < minimo:
        return None

    horas = [(p.get("holding_seconds") or 0) / HORA for p in posiciones]
    aguantaron = sum(1 for h in horas if h >= hold_h)
    ganadoras = sum(1 for p in posiciones if (p.get("realized_pnl") or 0) > 0)
    mults = []
    for p in posiciones:
        e = float(p.get("sol_in") or 0)
        s = float(p.get("sol_out") or 0)
        if e > 0:
            mults.append(s / e)
    rois = [p.get("roi_pct") for p in posiciones]

    q_hold = 100.0 * aguantaron / n
    q_consistency = 100.0 * ganadoras / n
    q_profit = _pata_profit(_mediana(mults))
    q_risk = _pata_riesgo(rois)

    # Supervivencia: SOLO sobre los tokens donde se sabe. "No se sabe"
    # no es "no sobrevivio" (fase 4).
    sabidos = [p for p in posiciones if p.get("survived") in (0, 1)]
    q_survival = (100.0 * sum(p["survived"] for p in sabidos) / len(sabidos)
                  if sabidos else None)

    valores = {"q_hold": q_hold, "q_consistency": q_consistency,
               "q_profit": q_profit, "q_survival": q_survival,
               "q_risk": q_risk}
    # Media ponderada saltandose las patas que no se pudieron medir: su
    # peso se reparte entre las demas en vez de contar como cero.
    suma = peso_total = 0.0
    for clave, nombre_peso in PATAS:
        v = valores.get(clave)
        if v is None:
            continue
        peso = _cfg(nombre_peso, 20.0)
        suma += v * peso
        peso_total += peso
    q_score = (suma / peso_total) if peso_total > 0 else None

    mediana_h = _mediana(horas)
    if mediana_h is None:
        estrategia = "?"
    elif mediana_h >= hold_h:
        estrategia = "aguanta"
    elif mediana_h < 1.0:
        estrategia = "voltea"
    else:
        estrategia = "mixta"
    # q_strategy: 100 para quien aguanta, 0 para quien voltea en menos de
    # una hora. Se guarda aparte de q_hold porque mide la mediana (como
    # opera NORMALMENTE) y no el recuento.
    if mediana_h is None:
        q_strategy = None
    else:
        q_strategy = _acotar(100.0 * min(1.0, mediana_h / hold_h))

    if n >= minimo * 3:
        conf = "alta"
    elif n >= minimo:
        conf = "media"
    else:
        conf = "baja"

    return {
        "q_score": q_score, "q_consistency": q_consistency,
        "q_profit": q_profit, "q_survival": q_survival, "q_hold": q_hold,
        "q_risk": q_risk, "q_strategy": q_strategy,
        "q_ts": int(time.time()), "estrategia": estrategia,
        "hold_median_h": mediana_h, "mult_realizado": _mediana(mults),
        "tok_500k": len({p["mint"] for p in posiciones
                         if p.get("hito_500k")}),
        "tok_1m": len({p["mint"] for p in posiciones if p.get("hito_1m")}),
        "tok_surv": len({p["mint"] for p in posiciones
                         if p.get("survived") == 1}),
        "pos_24h": aguantaron, "pnl_confidence": conf,
    }


# ── lectura y escritura ───────────────────────────────────────────────

def posiciones_de(conn, wallet: str) -> list[dict]:
    """Las posiciones MEDIBLES de una billetera, con la supervivencia y
    los hitos del token pegados (una sola consulta)."""
    nivel_500 = _cfg("MIN_WINNER_MC", 500_000)
    nivel_1m = _cfg("BREAKOUT_MC", 1_000_000)
    try:
        filas = conn.execute(
            """SELECT p.mint, p.sol_in, p.sol_out, p.realized_pnl,
                      p.roi_pct, p.holding_seconds,
                      w.survival_24h AS survived,
                      (SELECT COUNT(*) FROM token_milestones m
                        WHERE m.mint = p.mint AND m.milestone_usd = ?) h500,
                      (SELECT COUNT(*) FROM token_milestones m
                        WHERE m.mint = p.mint AND m.milestone_usd = ?) h1m
               FROM wallet_positions p
               LEFT JOIN winning_tokens w ON w.mint = p.mint
               WHERE p.wallet = ? AND p.history_complete = 1
                 AND p.position_status = 'cerrada'""",
            (nivel_500, nivel_1m, wallet)).fetchall()
    except Exception as _ex:
        _avisar_ex("wallet_quality:posiciones_de", _ex)
        return []
    out = []
    for f in filas:
        d = dict(f)
        d["hito_500k"] = bool(d.pop("h500", 0))
        d["hito_1m"] = bool(d.pop("h1m", 0))
        out.append(d)
    return out


def guardar(conn, wallet: str, nota: dict) -> bool:
    """Escribe las columnas `q_*` de esa billetera. No toca `score`,
    `is_tracked` ni `grade`: la nota vieja sigue mandando hasta que el
    dueño decida lo contrario."""
    if not wallet or not nota:
        return False
    try:
        sets = ", ".join(f"{c} = ?" for c in COLUMNAS)
        conn.execute(f"UPDATE wallets SET {sets} WHERE address = ?",
                     (*[nota.get(c) for c in COLUMNAS], wallet))
        return True
    except Exception as _ex:
        _avisar_ex("wallet_quality:guardar", _ex)
        return False


def _a_puntuar(conn, limite: int) -> list[str]:
    """Billeteras con material suficiente y sin nota reciente."""
    minimo = int(_cfg("CALIDAD_MIN_POSICIONES", 3))
    frescura = int(_cfg("CALIDAD_REFRESCO_H", 24)) * HORA
    corte = int(time.time()) - frescura
    try:
        filas = conn.execute(
            """SELECT p.wallet, COUNT(*) n FROM wallet_positions p
               LEFT JOIN wallets w ON w.address = p.wallet
               WHERE p.history_complete = 1 AND p.position_status = 'cerrada'
                 AND (w.q_ts IS NULL OR w.q_ts < ?)
               GROUP BY p.wallet HAVING COUNT(*) >= ?
               ORDER BY n DESC LIMIT ?""",
            (corte, minimo, int(limite))).fetchall()
        return [f["wallet"] for f in filas]
    except Exception as _ex:
        _avisar_ex("wallet_quality:_a_puntuar", _ex)
        return []


def puntuar(limite: int | None = None) -> int:
    """Pasada periodica. Cero creditos de Helius: todo sale de
    `wallet_positions`, que a su vez salio del historial propio."""
    if limite is None:
        limite = int(_cfg("CALIDAD_WALLETS_POR_PASADA", 200))
    conn = get_conn()
    try:
        wallets = _a_puntuar(conn, limite)
        if not wallets:
            print("· Calidad: ninguna billetera nueva que puntuar")
            return 0
        hold_h = _cfg("HOLD_MIN_HOURS", 24.0)
        n = aguantan = 0
        for w in wallets:
            nota = calcular(posiciones_de(conn, w), hold_h)
            if nota and guardar(conn, w, nota):
                n += 1
                aguantan += (nota["estrategia"] == "aguanta")
        conn.commit()
        try:
            from analysis_events import registrar
            registrar(conn, "lote", "calidad", "calidad", "puntuadas",
                      f"{n} billeteras · {aguantan} aguantan",
                      score=n, data_source="wallet_positions")
        except Exception as _ex:
            _avisar_ex("wallet_quality:puntuar:evento", _ex)
        print(f"✓ Calidad: {n} billeteras puntuadas ({aguantan} aguantan "
              f"≥{hold_h:g} h) · 0 creditos de Helius")
        return n
    finally:
        conn.close()


# ── texto para Telegram ───────────────────────────────────────────────

def mejores(conn, limite: int = 15, solo_aguantan: bool = True) -> list[dict]:
    try:
        extra = "AND estrategia = 'aguanta' " if solo_aguantan else ""
        return [dict(f) for f in conn.execute(
            f"""SELECT address, q_score, q_hold, q_consistency, q_profit,
                       q_survival, q_risk, estrategia, hold_median_h,
                       mult_realizado, pos_24h, tok_1m, pnl_confidence,
                       COALESCE(is_tracked,0) is_tracked
                FROM wallets WHERE q_score IS NOT NULL {extra}
                ORDER BY q_score DESC LIMIT ?""", (int(limite),)).fetchall()]
    except Exception as _ex:
        _avisar_ex("wallet_quality:mejores", _ex)
        return []


def eficientes(conn, limite: int = 15) -> list[dict]:
    """Las que MULTIPLICAN metiendo poco: el criterio literal del dueño.

    No ordena por PnL absoluto —eso corona a quien mete 130 SOL para
    sacar 205— sino por el multiplo TIPICO de quien opera con capital
    pequeño. El suelo de capital no es un adorno: con posiciones de
    milesimas de SOL el multiplo se dispara sin significar nada (en la
    base hay un "x1.358" que metio 0,004 SOL).

    Se calcula sobre `wallet_positions` en el momento de preguntar, y no
    se guarda en ninguna columna: es una forma de MIRAR lo ya medido, no
    una nota nueva que pueda quedarse vieja.
    """
    minimo = _cfg("DESCUBRIMIENTO_MIN_SOL", 0.5)
    maximo = _cfg("DESCUBRIMIENTO_MAX_SOL", 5.0)
    minpos = int(_cfg("CALIDAD_MIN_POSICIONES", 3))
    try:
        filas = conn.execute(
            """SELECT p.wallet, COUNT(*) n,
                      AVG(p.sol_in) capital,
                      SUM(p.sol_out) / SUM(p.sol_in) mult,
                      SUM(CASE WHEN p.held_24h = 1 THEN 1 ELSE 0 END) agu,
                      SUM(p.realized_pnl) pnl,
                      MAX(COALESCE(w.q_score, -1)) q,
                      MAX(COALESCE(w.is_tracked, 0)) est
               FROM wallet_positions p
               LEFT JOIN wallets w ON w.address = p.wallet
               WHERE p.history_complete = 1
                 AND p.position_status = 'cerrada'
                 AND p.sol_in >= ? AND p.sol_in <= ?
               GROUP BY p.wallet
               HAVING COUNT(*) >= ? AND SUM(p.sol_in) > 0
               ORDER BY mult DESC LIMIT ?""",
            (minimo, maximo, minpos, int(limite))).fetchall()
        return [dict(f) for f in filas]
    except Exception as _ex:
        _avisar_ex("wallet_quality:eficientes", _ex)
        return []


def eficientes_text(conn, limite: int = 12) -> str:
    minimo = _cfg("DESCUBRIMIENTO_MIN_SOL", 0.5)
    maximo = _cfg("DESCUBRIMIENTO_MAX_SOL", 5.0)
    filas = eficientes(conn, limite)
    L = [f"💎 *Multiplican metiendo poco* ({minimo:g}-{maximo:g} SOL "
         f"por operación)", ""]
    if not filas:
        L.append("Todavía no hay ninguna con suficientes operaciones "
                 "medidas en esa banda de capital.")
        return "\n".join(L)
    for f in filas:
        est = " ⭐" if f["est"] else ""
        nota = (f" · nota {f['q']:.0f}" if (f["q"] or -1) >= 0 else "")
        L.append(f"`{f['wallet'][:10]}…`{est} *x{(f['mult'] or 0):.1f}* "
                 f"con {(f['capital'] or 0):.1f} SOL típicos · "
                 f"{f['n']} ops ({f['agu']} aguantadas)"
                 f" · {(f['pnl'] or 0):+.1f} SOL{nota}")
    L.append("")
    L.append("_El múltiplo es del dinero puesto en esa banda, no del "
             "total de la billetera._")
    return "\n".join(L)


def resumen_text(conn=None, limite: int = 10) -> str:
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        try:
            tot = conn.execute("SELECT COUNT(*) c FROM wallets WHERE "
                               "q_score IS NOT NULL").fetchone()["c"]
        except Exception as _ex:
            _avisar_ex("wallet_quality:resumen_text:total", _ex)
            return "🏅 *Calidad*\n\nTodavía no hay ninguna billetera puntuada."
        if not tot:
            return ("🏅 *Calidad*\n\nTodavía no hay ninguna puntuada. Se "
                    "llenan solas con las posiciones ya reconstruidas, sin "
                    "gastar créditos.")
        L = [f"🏅 *Calidad de billeteras* — {tot} puntuadas", ""]
        for f in conn.execute(
                "SELECT estrategia e, COUNT(*) n FROM wallets WHERE "
                "q_score IS NOT NULL GROUP BY e ORDER BY n DESC").fetchall():
            L.append(f"  · {f['e'] or '?'}: {f['n']}")
        filas = mejores(conn, limite, solo_aguantan=True)
        if filas:
            L.append("")
            L.append("*Las que AGUANTAN y mejor puntúan*")
            for f in filas:
                est = " ⭐" if f["is_tracked"] else ""
                mult = (f"x{f['mult_realizado']:.1f}"
                        if f["mult_realizado"] is not None else "?")
                L.append(f"`{f['address'][:10]}…`{est} nota "
                         f"{(f['q_score'] or 0):.0f} · {mult} típico · "
                         f"{(f['hold_median_h'] or 0):.0f} h dentro · "
                         f"{f['pos_24h']} aguantes")
        else:
            L.append("")
            L.append("_Ninguna con estrategia de aguantar todavía._")
        L.append("")
        L.append("_`/calidad todas` incluye a las que voltean · "
                 "`/calidad poco` las que multiplican con poco capital._")
        return "\n".join(L)
    finally:
        if propia:
            conn.close()


def detalle_text(conn, wallet: str) -> str:
    """Las cinco patas de una billetera, para poder discutir la nota."""
    try:
        f = conn.execute(
            """SELECT address, q_score, q_hold, q_consistency, q_profit,
                      q_survival, q_risk, q_strategy, estrategia,
                      hold_median_h, mult_realizado, pos_24h, tok_500k,
                      tok_1m, tok_surv, pnl_confidence
               FROM wallets WHERE address = ?""", (wallet,)).fetchone()
    except Exception as _ex:
        _avisar_ex("wallet_quality:detalle_text", _ex)
        return "No pude leer esa billetera."
    if not f or f["q_score"] is None:
        return (f"`{wallet[:12]}…` todavía no tiene nota de calidad: hacen "
                f"falta al menos {int(_cfg('CALIDAD_MIN_POSICIONES', 3))} "
                f"posiciones cerradas y medidas.")
    def _p(v):
        return f"{v:.0f}" if v is not None else "no se sabe"
    L = [f"🏅 `{f['address'][:14]}…` — nota *{f['q_score']:.0f}*",
         f"_{f['estrategia']} · confianza {f['pnl_confidence']}_", "",
         f"  Aguantar ≥ un día ... {_p(f['q_hold'])}",
         f"  Acertar a menudo ... {_p(f['q_consistency'])}",
         f"  Cuánto multiplica .. {_p(f['q_profit'])}",
         f"  Tokens que viven ... {_p(f['q_survival'])}",
         f"  Poco daño al fallar  {_p(f['q_risk'])}", "",
         f"Operación típica: x{(f['mult_realizado'] or 0):.2f} en "
         f"{(f['hold_median_h'] or 0):.0f} h · {f['pos_24h']} aguantes",
         f"Tokens: {f['tok_500k']} de ≥500K · {f['tok_1m']} de ≥1M · "
         f"{f['tok_surv']} siguen vivos"]
    return "\n".join(L)


if __name__ == "__main__":
    puntuar()
