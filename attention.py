"""
Score de Atención + Priority Score + presupuesto de atención.

La atención es un recurso ESCASO: no todas las wallets merecen el mismo
análisis. Dos ideas clave:

  · Attention Score (0-100): separado del de rentabilidad. Mide cuánto
    DESCUBRE una wallet — entra antes que nadie, lidera, otras la siguen.
    Una wallet muy rentable pero que solo COPIA aporta poca info nueva
    (atención baja). Una que descubre proyectos temprano vale mucho para
    vigilar aunque su PnL sea menor.

  · Priority Score (0-100): combina rentabilidad (wallet_score, dominado
    por PnL) + atención. Ordena a las ⭐ para el PRESUPUESTO: solo caben
    MAX_ELITE en seguimiento activo; si aparece una mejor, sale la peor.

Todo se calcula de señales ya existentes (alpha, originalidad, influencia).
"""

from db import get_conn
from avisos import aviso as _avisar_ex   # (19-AE)


def attention_score(address: str, inf=None, ap=None) -> int:
    """0-100. Alto = descubre y lidera; bajo = copia/aislada."""
    try:
        if ap is None:
            from alpha import alpha_profile
            ap = alpha_profile(address)
    except Exception as _ex:
        _avisar_ex("attention:attention_score:29", _ex)
        ap = None
    try:
        if inf is None:
            from influence import influence as _inf
            inf = _inf(address)
    except Exception as _ex:
        _avisar_ex("attention:attention_score:35", _ex)
        inf = None
    ap = ap or {}
    inf = inf or {}
    f_alpha = (ap.get("alpha_score") or 0) / 100.0
    f_orig = (ap.get("originality") or 0) / 100.0
    f_lead = (inf.get("leader_score") or 0) / 100.0
    f_foll = min(1.0, (inf.get("followers_count") or 0) / 5.0)
    score = 100 * (0.30 * f_alpha + 0.25 * f_orig +
                   0.25 * f_lead + 0.20 * f_foll)
    return round(score)


def priority_score(address: str, wallet_score=None, inf=None, ap=None) -> int:
    """0-100. Rentabilidad + atención → decide el presupuesto de ⭐.

    wallet_score NULL cuenta como 0 (v2, auditoria 19/8): antes valía 50
    y una billetera SIN medir protegía su cupo por delante de una medida
    mala — lo no medido no puede puntuar mejor que lo medido."""
    att = attention_score(address, inf, ap)
    f_prof = min(1.0, (wallet_score if wallet_score is not None else 0)
                 / 100.0)
    f_att = att / 100.0
    return round(100 * (0.50 * f_prof + 0.50 * f_att))


def format_attention(address: str, wallet_score=None) -> str | None:
    att = attention_score(address)
    if att <= 0:
        return None
    pri = priority_score(address, wallet_score)
    tag = ("descubridora" if att >= 70 else "seguidora" if att <= 30
           else "mixta")
    return f"🎯 Atención: *{att}/100* ({tag}) · Prioridad: {pri}/100"


def enforce_budget(conn, max_elite: int) -> int:
    """
    Presupuesto de atención: mantiene como ⭐ solo a las MAX_ELITE de mayor
    Priority Score. Las que sobran descienden (is_tracked=0) con motivo.
    Devuelve cuántas descendió. Barato si no se supera el tope.
    """
    stars = conn.execute(
        "SELECT address, wallet_score FROM wallets WHERE is_tracked=1"
    ).fetchall()
    if len(stars) <= max_elite:
        return 0
    ranked = sorted(
        stars,
        key=lambda r: priority_score(r["address"], r["wallet_score"]),
        reverse=True)
    to_demote = ranked[max_elite:]
    for r in to_demote:
        # ai_follow=0 TAMBIEN (v2, auditoria 19/8): degradar solo con
        # is_tracked=0 dejaba ai_follow=1 y la siguiente re-evaluacion
        # re-estrellaba a la billetera — flip-flop eterno y el tope de
        # presupuesto nunca aplicaba. Y el motivo va con SUBSTR(…,500)
        # como el resto de escritores, para no crecer sin tope.
        conn.execute(
            "UPDATE wallets SET is_tracked=0, ai_follow=0, "
            "confirmada=0, prueba_desde=NULL, turno_desde=NULL, "
            "ai_reason=SUBSTR(COALESCE(ai_reason,'') || ?, 1, 500) "
            "WHERE address=?",
            (" · descendida por presupuesto de atención", r["address"]))
    conn.commit()
    try:
        from db import invalidar_copiables
        invalidar_copiables()
    except Exception as _ex:
        _avisar_ex("attention:enforce_budget:103", _ex)
        pass
    return len(to_demote)


def top_attention(limit: int = 10) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT address, alias, wallet_score, grade FROM wallets "
            "WHERE COALESCE(is_bot,0)=0").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        att = attention_score(r["address"])
        if att <= 0:
            continue
        out.append({"address": r["address"],
                    "alias": r["alias"] or r["address"][:6],
                    "attention": att, "grade": r["grade"],
                    "priority": priority_score(r["address"], r["wallet_score"])})
    out.sort(key=lambda x: x["attention"], reverse=True)
    return out[:limit]


def attention_text(limit: int = 10) -> str:
    top = top_attention(limit)
    if not top:
        return ("🎯 Aún no puedo medir la atención: hacen falta más co-compras "
                "para calcular alpha/originalidad/liderazgo.")
    out = ["🎯 *Score de Atención* — quién aporta info nueva\n",
           "_Separado de la rentabilidad: mide descubrimiento y liderazgo._\n"]
    for i, w in enumerate(top, 1):
        g = f" [{w['grade']}]" if w["grade"] else ""
        out.append(f"{i}. *{w['alias']}*{g} — atención {w['attention']} · "
                   f"prioridad {w['priority']}")
    out.append("\n_Las de mayor atención generan más oportunidades nuevas; "
               "el bot les da prioridad de seguimiento._")
    return "\n".join(out)
