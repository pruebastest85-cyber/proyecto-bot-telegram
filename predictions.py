"""
Motor de decisiones predictivo (no un simple disparador de mensajes).

Flujo:
  1. Una billetera ⭐ compra un token.
  2. Si es LÍDER (tiene seguidores fuertes en el grafo de influencia) y
     no hay predicción abierta para ese token → se crea una predicción:
     quién comprará detrás, con qué probabilidad y en cuánto tiempo.
  3. Se calcula un CONFIDENCE SCORE (combina historial, estabilidad del
     patrón, fuerza del líder, calidad del cluster, liquidez y salud
     reciente del líder) y un META SCORE ponderado de la señal.
  4. Solo si supera el umbral configurado (`pred_min_confidence`, 85 por
     defecto) se emite la alerta.
  5. PREDICCIÓN POR ETAPAS: cada seguidor esperado que va llegando sube
     la etapa (Nivel 1 → 2 → 3) y recalcula la confianza.
  6. APRENDIZAJE: al cerrarse la ventana, la predicción se evalúa sola
     (cuántos seguidores llegaron) y alimenta la salud del líder.

Todo es best-effort: si algo falla, nunca rompe el flujo de señales.
"""

import json
import time

from db import get_conn, get_setting
from influence import influence, graph

WINDOW_MIN = 20          # ventana para que lleguen los seguidores
EVAL_AFTER_MIN = 30      # a partir de aquí la predicción se evalúa sola
DEFAULT_MIN_CONF = 85    # umbral de confianza para alertar
ALPHA_META = 90          # Meta Score mínimo para 🟢 Alpha
WATCH_META = 70          # Meta Score mínimo para 🟡 Watchlist


MIN_LIQ_USD = 20000      # liquidez mínima; por debajo = 🔴 Ignorada
MAX_RISK = 70            # riesgo (concentración/mint) máximo antes de Ignorar


def _tier(conf: int, meta: int, umbral: int,
          liq=None, risk=None) -> str:
    """🟢 alpha / 🟡 watchlist / 🔴 ignored. Filtros duros primero."""
    if liq is not None and (liq or 0) < MIN_LIQ_USD:
        return "ignored"          # baja liquidez
    if risk is not None and (risk or 0) >= MAX_RISK:
        return "ignored"          # riesgo elevado (rug/concentración)
    if meta >= ALPHA_META and conf >= umbral:
        return "alpha"
    if meta >= WATCH_META and conf >= 60:
        return "watchlist"
    return "ignored"


_TIER_BADGE = {"alpha": "🟢 ALPHA ALERT", "watchlist": "🟡 WATCHLIST",
               "ignored": "🔴 Ignorada"}


def _should_push(tier: str, conn) -> bool:
    if tier == "alpha":
        return True
    if tier == "watchlist":
        return (get_setting(conn, "pred_send_watchlist", "0") or "0") == "1"
    return False


# ─────────────────────────── SCORING ────────────────────────────────

def _leader_health(conn, leader: str) -> dict:
    """Precisión de las predicciones del líder en los últimos 30 días."""
    since = int(time.time()) - 30 * 86400
    rows = conn.execute(
        """SELECT outcome_pct FROM predictions
           WHERE leader=? AND status='evaluada' AND evaluated_ts>=?""",
        (leader, since)).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0, "accuracy": None, "estado": "sin historial", "factor": 0.6}
    acc = sum((r["outcome_pct"] or 0) for r in rows) / n
    if n >= 5 and acc >= 70:
        estado, factor = "🟢 Caliente", 1.0
    elif acc < 50:
        estado, factor = "🔴 En declive", 0.3
    else:
        estado, factor = "🟡 Normal", 0.65
    return {"n": n, "accuracy": round(acc), "estado": estado, "factor": factor}


def confidence_score(inf: dict, followers: list, liq, health: dict,
                     arrived: int = 0) -> int:
    """
    0-100. Combina, con pesos:
      historial (25) · estabilidad del patrón (20) · fuerza del líder (15)
      liquidez (10) · salud reciente del líder (15) · confirmación por
      etapas (15, sube según cuántos seguidores ya llegaron).
    """
    if not followers:
        return 0
    shared = [f.get("shared", 0) for f in followers]
    probs = [f.get("prob", 0) / 100 for f in followers]
    f_hist = min(1.0, (sum(shared) / len(shared)) / 8.0)      # ≥8 coincid. = tope
    f_stab = sum(probs) / len(probs)                           # prob media edges
    f_lead = (inf.get("leader_score") or 0) / 100
    f_liq = 1.0 if (liq or 0) >= 20000 else max(0.0, (liq or 0) / 20000)
    f_health = health.get("factor", 0.6)
    if arrived > 0:
        f_stage = min(1.0, arrived / max(1, len(followers)))   # etapas
    else:
        # Etapa 1 (sin confirmaciones aun): valor neutro. Con 0, el maximo
        # teorico era 85 y el umbral por defecto (85) hacia practicamente
        # imposible alertar en la etapa inicial.
        f_stage = 0.5
    score = (25 * f_hist + 20 * f_stab + 15 * f_lead +
             10 * f_liq + 15 * f_health + 15 * f_stage)
    return round(min(100.0, score))


def meta_score(inf: dict, cluster: dict | None, health: dict,
               followers: list, liq, risk_pct) -> int:
    """
    Meta Score 0-100 de la señal (pesos del diseño):
      Leader 20 · Cluster 20 · Historial líder 15 · Propagación 20 ·
      Liquidez 10 · Riesgo 5 · (Historial token 10 → neutro si no hay).
    """
    f_lead = (inf.get("leader_score") or 0) / 100
    if cluster:
        f_cluster = min(1.0, (cluster.get("shared_tokens", 0) / 6.0) *
                        (cluster.get("size", 0) / 5.0) + 0.2)
        f_cluster = min(1.0, f_cluster)
    else:
        f_cluster = 0.3
    f_hist = health.get("factor", 0.6)
    probs = [f.get("prob", 0) / 100 for f in followers] or [0]
    f_prop = sum(probs) / len(probs)
    f_liq = 1.0 if (liq or 0) >= 20000 else max(0.0, (liq or 0) / 20000)
    f_token = 0.6      # neutro: sin histórico propio del token
    f_risk = 1.0 - min(1.0, (risk_pct or 0) / 100)   # menos riesgo = mejor
    score = (20 * f_lead + 20 * f_cluster + 15 * f_hist + 20 * f_prop +
             10 * f_liq + 10 * f_token + 5 * f_risk)
    return round(min(100.0, score))


# ─────────────────────────── MOTOR ──────────────────────────────────

def _send(text: str):
    try:
        from realtime import tg_send
        tg_send(text)
    except Exception as e:
        print(f"· Predicción: no pude enviar alerta: {e}")


def _risk_pct(token_ctx: dict) -> float:
    """Aproxima el riesgo del token (0-100) desde RugCheck/holders."""
    top10 = token_ctx.get("top10_pct")
    r = 0.0
    if top10 is not None:
        r = max(r, min(100.0, float(top10)))
    if token_ctx.get("mint_auth"):
        r = max(r, 60.0)
    return r


def _alert_stage(pred_row, inf, conf, meta, followers, health, token_ctx):
    stage = pred_row["stage"]
    liq = token_ctx.get("liq")
    sym = token_ctx.get("symbol", "?")
    niveles = {1: "Nivel 1 · líder compró",
               2: "Nivel 2 · seguidor confirmó",
               3: "Nivel 3 · cluster propagando"}
    nivel = niveles.get(min(stage, 3), f"Nivel {stage}")
    alias = graph()["wallets"].get(pred_row["leader"], {}).get("alias",
                                                               pred_row["leader"][:6])
    badge = _TIER_BADGE.get(pred_row["tier"] or "", "")
    lines = [f"{badge}",
             f"🔮 *SEÑAL PREDICTIVA — {nivel}*",
             f"Líder: *{alias}* · Token: `{sym}`",
             f"Confianza: *{conf}%* · Meta Score: *{meta}/100*"]
    if health.get("accuracy") is not None:
        lines.append(f"Salud del líder (30d): {health['estado']} "
                     f"({health['accuracy']}% en {health['n']})")
    prox = [f for f in followers if f["prob"] >= 60][:6]
    if prox:
        lines.append("Se espera que entren:")
        for f in prox:
            eta = f"{f['eta_s']}s" if (f.get('eta_s') or 0) < 90 else \
                  f"{round((f.get('eta_s') or 0)/60)}min"
            lines.append(f"• {f['alias']} — {f['prob']}% · ~{eta}")
    if liq:
        lines.append(f"Liquidez: ${liq:,.0f}")
    lines.append("\n_Alerta emitida por superar el umbral de confianza._")
    _send("\n".join(lines))


def on_buy(conn, wallet: str, mint: str, ts: int, token_ctx: dict):
    """Punto de entrada desde realtime cuando una ⭐ compra."""
    try:
        evaluate_due(conn)          # cierra predicciones vencidas primero
    except Exception as e:
        print(f"· Predicción eval: {e}")

    umbral = DEFAULT_MIN_CONF
    try:
        umbral = int(float(get_setting(conn, "pred_min_confidence",
                                       str(DEFAULT_MIN_CONF)) or DEFAULT_MIN_CONF))
    except (TypeError, ValueError):
        pass

    # ¿Hay ya una predicción abierta para este token?
    row = conn.execute(
        "SELECT * FROM predictions WHERE mint=? AND status='abierta' "
        "ORDER BY created_ts DESC LIMIT 1", (mint,)).fetchone()

    if row:
        # ¿Este comprador es un seguidor esperado? → sube de etapa
        pred = json.loads(row["predicted"] or "[]")
        pred_w = {p["wallet"] for p in pred}
        if wallet in pred_w and wallet != row["leader"]:
            arrived = set(json.loads(row["arrived"] or "[]"))
            if wallet not in arrived:
                first = None
                if not arrived:      # primer seguidor en llegar
                    first = max(0, int(ts) - int(row["created_ts"] or ts))
                arrived.add(wallet)
                inf = influence(row["leader"]) or {}
                health = _leader_health(conn, row["leader"])
                conf = confidence_score(inf, pred, token_ctx.get("liq"),
                                        health, arrived=len(arrived))
                stage = 1 + len(arrived)
                tier = _tier(conf, row["meta_score"] or 0, umbral,
                             token_ctx.get("liq"), _risk_pct(token_ctx))
                conn.execute(
                    "UPDATE predictions SET arrived=?, stage=?, confidence=?, "
                    "tier=?, first_confirm_s=COALESCE(first_confirm_s,?) "
                    "WHERE id=?",
                    (json.dumps(sorted(arrived)), stage, conf, tier,
                     first, row["id"]))
                conn.commit()
                if _should_push(tier, conn) and stage > (row["alerted_stage"] or 0):
                    conn.execute(
                        "UPDATE predictions SET alerted_stage=? WHERE id=?",
                        (stage, row["id"]))
                    conn.commit()
                    fresh = conn.execute(
                        "SELECT * FROM predictions WHERE id=?",
                        (row["id"],)).fetchone()
                    _alert_stage(fresh, inf, conf,
                                 row["meta_score"], pred, health, token_ctx)
        return

    # No hay predicción abierta: ¿este comprador es LÍDER con seguidores?
    inf = influence(wallet)
    if not inf or not inf.get("followers"):
        return
    followers = [f for f in inf["followers"] if f["prob"] >= 60]
    if len(followers) < 2:
        return

    health = _leader_health(conn, wallet)
    from clusters import cluster_for
    cluster = None
    try:
        cluster = cluster_for(wallet)
    except Exception:
        pass
    conf = confidence_score(inf, followers, token_ctx.get("liq"), health)
    meta = meta_score(inf, cluster, health, followers,
                      token_ctx.get("liq"), _risk_pct(token_ctx))

    tier = _tier(conf, meta, umbral, token_ctx.get("liq"), _risk_pct(token_ctx))
    cur = conn.execute(
        """INSERT OR IGNORE INTO predictions
           (leader, mint, created_ts, stage, confidence, meta_score,
            predicted, arrived, alerted_stage, status, tier, price0)
           VALUES (?,?,?,?,?,?,?,?,0,'abierta',?,?)""",
        (wallet, mint, ts, 1, conf, meta,
         json.dumps(followers), json.dumps([]), tier,
         token_ctx.get("price")))
    conn.commit()
    # Si ya existía una predicción para (leader, mint) — p.ej. una vieja ya
    # evaluada — el INSERT no crea nada; no re-alertamos sobre datos rancios.
    if not cur.rowcount:
        return

    if _should_push(tier, conn):
        conn.execute(
            "UPDATE predictions SET alerted_stage=1 "
            "WHERE leader=? AND mint=? AND status='abierta'", (wallet, mint))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM predictions WHERE leader=? AND mint=? "
            "AND status='abierta'", (wallet, mint)).fetchone()
        _alert_stage(row, inf, conf, meta, followers, health, token_ctx)


def evaluate_due(conn):
    """Cierra y evalúa las predicciones cuya ventana ya venció."""
    cutoff = int(time.time()) - EVAL_AFTER_MIN * 60
    rows = conn.execute(
        "SELECT * FROM predictions WHERE status='abierta' AND created_ts<=?",
        (cutoff,)).fetchall()
    for r in rows:
        pred = json.loads(r["predicted"] or "[]")
        arrived = set(json.loads(r["arrived"] or "[]"))
        pred_w = [p["wallet"] for p in pred]
        outcome = round(100 * sum(1 for w in pred_w if w in arrived) /
                        len(pred_w)) if pred_w else 0
        conn.execute(
            "UPDATE predictions SET status='evaluada', outcome_pct=?, "
            "evaluated_ts=? WHERE id=?",
            (outcome, int(time.time()), r["id"]))
    if rows:
        conn.commit()


def fill_token_performance(conn, limit: int = 5):
    """
    Rellena token_chg_pct (rendimiento del token desde la predicción) de las
    predicciones ya evaluadas que aún no lo tienen. Hace llamadas de red, por
    eso SOLO se ejecuta desde el job periódico, nunca desde el webhook.
    """
    rows = conn.execute(
        "SELECT id, mint, price0 FROM predictions WHERE status='evaluada' "
        "AND token_chg_pct IS NULL AND price0 IS NOT NULL "
        "ORDER BY evaluated_ts DESC LIMIT ?", (limit,)).fetchall()
    for r in rows:
        try:
            from token_check import analyze_token
            now_px = analyze_token(r["mint"]).get("price")
            if now_px and r["price0"]:
                chg = round(100 * (now_px - r["price0"]) / r["price0"])
                conn.execute(
                    "UPDATE predictions SET token_chg_pct=? WHERE id=?",
                    (chg, r["id"]))
        except Exception:
            pass
    if rows:
        conn.commit()


def run_maintenance():
    """Punto de entrada del job periódico: evalúa vencidas y rellena el
    rendimiento de los tokens (fuera del camino del webhook)."""
    conn = get_conn()
    try:
        evaluate_due(conn)
        fill_token_performance(conn)
    finally:
        conn.close()


# ─────────────────────────── VISTAS ─────────────────────────────────

def leader_health_line(leader: str) -> str | None:
    conn = get_conn()
    try:
        h = _leader_health(conn, leader)
    finally:
        conn.close()
    if h["accuracy"] is None:
        return None
    return (f"Salud del líder (30d): {h['estado']} · "
            f"{h['accuracy']}% de acierto en {h['n']} predicciones")


def predictions_text(limit: int = 10) -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY created_ts DESC LIMIT ?",
            (limit,)).fetchall()
        ev = conn.execute(
            "SELECT COUNT(*) n, AVG(outcome_pct) a FROM predictions "
            "WHERE status='evaluada'").fetchone()
        gmap = graph()["wallets"]
    finally:
        conn.close()
    if not rows:
        return ("🔮 Aún no hay predicciones. El motor emite cuando una "
                "billetera líder (con seguidores fuertes) compra un token.")
    out = ["🔮 *Predicciones recientes*\n"]
    for r in rows:
        alias = gmap.get(r["leader"], {}).get("alias", r["leader"][:6])
        arrived = len(json.loads(r["arrived"] or "[]"))
        total = len(json.loads(r["predicted"] or "[]"))
        est = (f"✅ {r['outcome_pct']}% acierto" if r["status"] == "evaluada"
               else f"⏳ etapa {r['stage']} ({arrived}/{total})")
        out.append(f"• *{alias}* · conf {r['confidence']}% · "
                   f"meta {r['meta_score']} · {est}")
    if ev and ev["n"]:
        out.append(f"\n📊 Precisión global: {round(ev['a'] or 0)}% "
                   f"sobre {ev['n']} predicciones evaluadas")
    out.append("_El motor evalúa cada predicción sola y ajusta la salud de "
               "cada líder._")
    return "\n".join(out)


def metrics_text() -> str:
    """Panel interno de rendimiento del motor predictivo (/metricas)."""
    conn = get_conn()
    try:
        now = int(time.time())
        d1, d7 = now - 86400, now - 7 * 86400
        emit_1d = conn.execute(
            "SELECT COUNT(*) c FROM predictions WHERE created_ts>=?",
            (d1,)).fetchone()["c"]
        emit_7d = conn.execute(
            "SELECT COUNT(*) c FROM predictions WHERE created_ts>=?",
            (d7,)).fetchone()["c"]
        total = conn.execute(
            "SELECT COUNT(*) c FROM predictions").fetchone()["c"]
        n2 = conn.execute(
            "SELECT COUNT(*) c FROM predictions WHERE stage>=2").fetchone()["c"]
        n3 = conn.execute(
            "SELECT COUNT(*) c FROM predictions WHERE stage>=3").fetchone()["c"]
        ev = conn.execute(
            "SELECT COUNT(*) n, AVG(outcome_pct) acc, AVG(first_confirm_s) t, "
            "AVG(token_chg_pct) chg FROM predictions "
            "WHERE status='evaluada'").fetchone()
        fp = conn.execute(
            "SELECT COUNT(*) c FROM predictions "
            "WHERE status='evaluada' AND alerted_stage>0 AND outcome_pct=0"
        ).fetchone()["c"]
        alerted = conn.execute(
            "SELECT COUNT(*) c FROM predictions WHERE alerted_stage>0"
        ).fetchone()["c"]
        tiers = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) c FROM predictions GROUP BY tier").fetchall()}
        leaders = conn.execute(
            """SELECT leader, COUNT(*) n, AVG(outcome_pct) acc
               FROM predictions WHERE status='evaluada'
               GROUP BY leader HAVING COUNT(*)>=1
               ORDER BY acc DESC, n DESC LIMIT 5"""
        ).fetchall()
        gmap = graph()["wallets"]
    finally:
        conn.close()

    if total == 0:
        return ("📊 *Panel del motor predictivo*\n\nAún no hay predicciones "
                "registradas. Se irá poblando cuando billeteras líderes "
                "compren tokens. Vuelve en unos días para ver métricas reales.")

    def pct(a, b):
        return f"{round(100*a/b)}%" if b else "—"

    out = ["📊 *Panel del motor predictivo*\n",
           f"Emitidas: {emit_1d} hoy · {emit_7d} en 7d · {total} total",
           f"Alcanzan Nivel 2: {pct(n2, total)} · Nivel 3: {pct(n3, total)}"]
    if ev and ev["n"]:
        out.append(f"Precisión final: *{round(ev['acc'] or 0)}%* "
                   f"({ev['n']} evaluadas)")
        if ev["t"] is not None:
            out.append(f"Tiempo medio a confirmación: {round(ev['t'])}s")
        if ev["chg"] is not None:
            out.append(f"Rendimiento medio del token: {round(ev['chg']):+d}%")
        out.append(f"Falsos positivos (alertó y 0 llegó): {fp}/{alerted}")
    out.append(f"\nNiveles → 🟢 {tiers.get('alpha',0)} · "
               f"🟡 {tiers.get('watchlist',0)} · 🔴 {tiers.get('ignored',0)}")
    if leaders:
        out.append("\n*Líderes más fiables:*")
        for l in leaders:
            alias = gmap.get(l["leader"], {}).get("alias", l["leader"][:6])
            out.append(f"• {alias}: {round(l['acc'] or 0)}% ({l['n']} pred.)")
    out.append("\n_Usa estos datos para recalibrar umbrales y pesos con "
               "evidencia, no con intuición._")
    return "\n".join(out)
