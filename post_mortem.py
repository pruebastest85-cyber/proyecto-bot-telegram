"""
Post-mortem semanal: la IA revisa SUS PROPIAS decisiones contra el
resultado medido (Ola 11, 21/8/2026).

El sistema toma tres tipos de decisiones cuyo acierto queda medido en la
base: el filtro de entrada del paper (copiar/rechazar, modo sombra), el
A/B de salidas (gestión 'ia' vs 'reglas') y el rechazo de billeteras
evaluadas. Nadie cruzaba esas decisiones con lo que pasó después. Este
módulo lo hace cada semana:

  1. Junta los datos duros (aciertos, errores, dinero simulado dejado
     sobre la mesa) con consultas en vivo — los números se muestran
     SIEMPRE, haya IA o no.
  2. Se los pasa a la IA local (vía ia_puente) para que escriba 3-5
     hallazgos con nivel de confianza y UNA propuesta concreta cada uno.
  3. Las propuestas son SOLO sugerencias: aquí no se cambia ningún
     ajuste. El dueño decide (puede aplicarlas por chat, que ya pide
     confirmación y tiene deshacer).

/postmortem lo muestra al momento; el job semanal lo genera y lo manda.
"""

import json
import time

from db import get_conn, get_setting, set_setting


def _datos(conn) -> dict:
    """Los hechos medidos de los últimos 7 días (y acumulados donde
    aporta). Cada número sale de una consulta en vivo."""
    d: dict = {}
    hace7 = int(time.time()) - 7 * 86400

    # ── Filtro de entrada (modo sombra): ¿acertó copiar/rechazar? ──
    try:
        filas = conn.execute(
            """SELECT ia_entrada, COUNT(*) n, SUM(pnl_usd) pnl,
                      SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
                      SUM(CASE WHEN pnl_usd IS NOT NULL
                          THEN 1 ELSE 0 END) con_dato
               FROM paper_trades
               WHERE status <> 'abierta' AND ia_entrada IS NOT NULL
                 AND exit_ts >= ?
               GROUP BY ia_entrada""", (hace7,)).fetchall()
        d["filtro_entrada_7d"] = [
            {"veredicto": r["ia_entrada"], "n": r["n"],
             "pnl_usd": round(r["pnl"], 2) if r["pnl"] is not None else None,
             "wins": r["wins"], "con_dato": r["con_dato"]}
            for r in filas]
        # Errores concretos: lo copiado que perdió y lo rechazado que ganó
        d["peores_copiar"] = [
            {"symbol": r["symbol"], "pnl_usd": round(r["pnl_usd"], 2),
             "razon_ia": (r["ia_entrada_razon"] or "")[:90]}
            for r in conn.execute(
                """SELECT symbol, pnl_usd, ia_entrada_razon
                   FROM paper_trades
                   WHERE status <> 'abierta' AND ia_entrada = 'copiar'
                     AND pnl_usd < 0 AND exit_ts >= ?
                   ORDER BY pnl_usd ASC LIMIT 5""", (hace7,)).fetchall()]
        d["mejores_rechazadas"] = [
            {"symbol": r["symbol"], "pnl_usd": round(r["pnl_usd"], 2),
             "razon_ia": (r["ia_entrada_razon"] or "")[:90]}
            for r in conn.execute(
                """SELECT symbol, pnl_usd, ia_entrada_razon
                   FROM paper_trades
                   WHERE status <> 'abierta' AND ia_entrada = 'rechazar'
                     AND pnl_usd > 0 AND exit_ts >= ?
                   ORDER BY pnl_usd DESC LIMIT 5""", (hace7,)).fetchall()]
    except Exception as e:
        d["filtro_entrada_error"] = str(e)

    # ── A/B de salidas: gestión 'ia' vs 'reglas' (acumulado: la muestra
    # semanal suele ser corta y engañaría) ──
    try:
        filas = conn.execute(
            """SELECT gestion, COUNT(*) n, SUM(pnl_usd) pnl,
                      SUM(CASE WHEN pnl_usd IS NOT NULL
                          THEN 1 ELSE 0 END) con_dato
               FROM paper_trades
               WHERE status <> 'abierta' AND gestion IS NOT NULL
               GROUP BY gestion""").fetchall()
        d["ab_salidas_acumulado"] = [
            {"gestion": r["gestion"], "n": r["n"],
             "pnl_usd": round(r["pnl"], 2) if r["pnl"] is not None else None,
             "con_dato": r["con_dato"]} for r in filas]
    except Exception as e:
        d["ab_salidas_error"] = str(e)

    # ── Salidas decididas por la IA esta semana, por motivo ──
    try:
        d["salidas_ia_7d"] = [
            {"motivo": r["exit_reason"], "n": r["n"],
             "pnl_usd": round(r["pnl"], 2) if r["pnl"] is not None else None}
            for r in conn.execute(
                """SELECT exit_reason, COUNT(*) n, SUM(pnl_usd) pnl
                   FROM paper_trades
                   WHERE status <> 'abierta' AND decidido_por = 'ia_local'
                     AND exit_ts >= ?
                   GROUP BY exit_reason""", (hace7,)).fetchall()]
    except Exception as e:
        d["salidas_ia_error"] = str(e)

    # ── Rechazadas que siguieron acertando: billeteras evaluadas y NO
    # seguidas que después volvieron a aparecer temprano en ganadores ──
    try:
        d["rechazadas_que_siguen_acertando"] = [
            {"alias": r["alias"] or r["address"][:8],
             "clase_ia": r["ai_class"],
             "apariciones_tras_rechazo": r["c"]}
            for r in conn.execute(
                """SELECT w.address, w.alias, w.ai_class, COUNT(*) c
                   FROM wallets w JOIN appearances a ON a.wallet = w.address
                   WHERE COALESCE(w.is_bot, 0) = 0 AND w.is_tracked = 0
                     AND w.ai_class IS NOT NULL
                     AND w.pnl_updated IS NOT NULL
                     AND a.buy_time > w.pnl_updated
                   GROUP BY w.address, w.alias, w.ai_class
                   HAVING COUNT(*) >= 2
                   ORDER BY c DESC LIMIT 6""").fetchall()]
    except Exception as e:
        d["rechazadas_error"] = str(e)

    # ── Señales de la semana: ¿el umbral silencia lo bueno? ──
    try:
        umbral = float(get_setting(conn, "min_signal_score", "0") or 0)
        r = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN chg_24h > 0 THEN 1 ELSE 0 END) wins,
                      AVG(chg_24h) media
               FROM signals WHERE side='compra' AND ts >= ?
                 AND chg_24h IS NOT NULL AND signal_score IS NOT NULL
                 AND signal_score < ?""", (hace7, umbral)).fetchone()
        d["senales_silenciadas_7d"] = {
            "umbral": umbral, "n": r["n"], "ganadoras": r["wins"],
            "chg24_medio_pct": round(r["media"], 1)
            if r["media"] is not None else None}
    except Exception as e:
        d["silenciadas_error"] = str(e)
    return d


def _datos_text(d: dict) -> str:
    """Los hechos, legibles, sin IA."""
    out = []
    fe = d.get("filtro_entrada_7d") or []
    if fe:
        trozos = []
        for r in fe:
            pnl = (f"{r['pnl_usd']:+.2f}$" if r["pnl_usd"] is not None
                   else "s/d")
            trozos.append(f"{r['veredicto']}: {r['n']} ({pnl})")
        out.append("🚪 Filtro de entrada (7d): " + " · ".join(trozos))
    for titulo, clave in (("🔴 Copió y perdió", "peores_copiar"),
                          ("🟡 Rechazó y ganó", "mejores_rechazadas")):
        filas = d.get(clave) or []
        if filas:
            out.append(titulo + ":")
            for r in filas[:3]:
                out.append(f"   · {r['symbol']}: {r['pnl_usd']:+.2f}$ — "
                           f"_{r['razon_ia'] or 'sin razón guardada'}_")
    ab = d.get("ab_salidas_acumulado") or []
    if len(ab) > 1:
        out.append("🤖 A/B salidas (acumulado): " + " vs ".join(
            f"{r['gestion']}: {r['n']} ops "
            + (f"{r['pnl_usd']:+.2f}$" if r["pnl_usd"] is not None
               else "s/d") for r in ab))
    rr = d.get("rechazadas_que_siguen_acertando") or []
    if rr:
        out.append("👀 Rechazadas que siguen apareciendo temprano en "
                   "ganadores:")
        for r in rr[:4]:
            out.append(f"   · {r['alias']} ({r['clase_ia']}): "
                       f"{r['apariciones_tras_rechazo']} apariciones "
                       f"tras el rechazo")
    sil = d.get("senales_silenciadas_7d")
    if sil and sil.get("n"):
        out.append(f"🔇 Silenciadas por umbral {sil['umbral']:.0f} (7d): "
                   f"{sil['n']} señales, {sil['ganadoras']} habrían "
                   f"ganado (chg24 medio "
                   f"{sil['chg24_medio_pct']:+.1f}%)")
    return "\n".join(out) if out else \
        "Aún no hay decisiones medidas suficientes esta semana."


PROMPT = """Eres el analista de control de calidad del sistema WALLET EDGE. Estos son los RESULTADOS MEDIDOS de las decisiones que el propio sistema (tú incluido) tomó. Revísalos con honestidad brutal.

DATOS MEDIDOS:
{datos}

Escribe 3-5 hallazgos en español. Reglas:
- Cada hallazgo: [confianza XX%] qué patrón ves en los datos + UNA propuesta concreta y pequeña (p. ej. "subir/bajar el umbral de señal", "revisar las razones del filtro de entrada sobre liquidez", "re-evaluar la billetera X").
- Basa TODO en los números de arriba; si la muestra es corta, dilo y baja la confianza.
- Son PROPUESTAS: el dueño decide. No des nada por aplicado.
- Sé específico con símbolos y cifras de los datos, no generalices.

Formato de cada línea:
• [confianza XX%] <hallazgo> → propuesta: <acción concreta>"""


def generar(conn=None) -> str:
    """Genera el informe (datos + comentario IA si la hay) y lo guarda."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        d = _datos(conn)
        cuerpo = _datos_text(d)
        comentario = ""
        try:
            from ia_puente import completar, hay_ia
            if hay_ia():
                prompt = PROMPT.format(datos=json.dumps(
                    d, ensure_ascii=False, indent=1)[:5000])
                texto = (completar(prompt, max_tokens=800,
                                   timeout=180) or "").strip()
                if texto:
                    comentario = ("\n\n🧠 *Lectura de la IA* "
                                  "(propuestas, no cambios):\n" + texto)
        except Exception as e:
            print(f"· Post-mortem: IA no disponible ({e})")
        informe = ("🔍 *Post-mortem semanal de decisiones*\n"
                   "_Lo decidido vs lo que pasó de verdad_\n\n"
                   + cuerpo + comentario)
        set_setting(conn, "post_mortem", informe[:3800])
        set_setting(conn, "post_mortem_ts",
                    time.strftime("%Y-%m-%d %H:%M"))
        return informe
    finally:
        if propia:
            conn.close()


def post_mortem_text(fresco: bool = False) -> str:
    """Para /postmortem: el guardado, o uno nuevo si se pide o no hay."""
    conn = get_conn()
    try:
        if not fresco:
            guardado = get_setting(conn, "post_mortem", None)
            ts = get_setting(conn, "post_mortem_ts", "")
            if guardado:
                return f"{guardado}\n\n_Generado: {ts} · /postmortem ya " \
                       f"para regenerarlo ahora_"
        return generar(conn)
    finally:
        conn.close()
