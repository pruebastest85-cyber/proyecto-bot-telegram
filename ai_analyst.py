"""
Analista IA: usa la API de Claude para clasificar billeteras candidatas.

v5:
  - Doble nivel: Haiku filtra rápido; si su confianza es baja (<65)
    se escala a un modelo más potente para el veredicto final.
  - Track record: la IA recibe la estadística real de las señales
    pasadas de la billetera (tasa de acierto a 1h/24h).
  - Re-evaluación semanal: los veredictos caducan a los 7 días y se
    renuevan con datos frescos; las que dejaron de ganar pierden la ⭐.
  - Más contexto: win rate, retención mediana y PnL 30d en el perfil.

Requiere la variable de entorno ANTHROPIC_API_KEY.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from db import now_iso
from wallet_profiler import profile_wallet

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_SMART = os.getenv("AI_SMART_MODEL", "claude-sonnet-5")
CONF_ESCALATE = 65        # confianza mínima de Haiku para no escalar
REEVAL_DAYS = 3           # caducidad del veredicto

PROMPT = """Eres un analista experto en trading on-chain de Solana. Analiza esta billetera candidata y clasifícala.

DATOS DEL PERFIL (últimas ~2000 transacciones):
{perfil}

TRACK RECORD REAL EN NUESTRO SISTEMA (resultado medido de sus señales pasadas; es el dato MÁS importante si existe):
{track}

EVIDENCIA (por qué está en nuestra base):
{evidencia}

Clasificaciones posibles:
- "trader": opera con criterio, PnL NETO positivo, ritmo humano. VALE seguirla.
- "sniper": entra muy temprano en lanzamientos con método consistente y gana. VALE seguirla.
- "insider": compra de primerísimo en tokens que luego explotan; probable dev/equipo. NO vale (no replicable).
- "bot": frecuencia inhumana, spray a decenas de tokens. NO vale.
- "mev_bot": flips en <1 minuto de forma sistemática (flips_menos_1min_pct alto), micro-ganancias en serie. NO vale (imposible copiar a mano).
- "market_maker": opera ambas direcciones del mismo token con posición neta ~0 (tokens_estilo_market_maker alto). NO vale (no direcciona precio).
- "copiador": parece replicar a otros con retraso. NO vale.
- "dev": billetera del equipo/creador del token (aparece de primerísimo y/o con mint authority). NO vale (no replicable).
- "vc": fondo o billetera grande de inversión; compras grandes, horizonte largo, pocos tokens. Informativo; normalmente NO copiable a mano.
- "influencer": mueve precio por audiencia, no por timing; entradas medianas seguidas de subida por volumen social. NO vale para copiar.
- "wallet_espejo": billetera secundaria de otra (mismo cluster/fondeo, mismas rutas y tamaños). Marca la relación; sigue a la principal, no a esta.
- "indeterminado": datos insuficientes.

Huellas clave de NO-humano: activa las 24 horas del día (horas_del_dia_activas_de_24 ≥ 22), compras de tamaño idéntico repetido (compras_tamano_identico_pct ≥ 70), flips <1 min. Un humano duerme, varía sus montos y tarda minutos u horas en vender.

Considera: track record real (si sus señales pasadas perdieron, NO vale aunque el perfil luzca bien), PnL NETO (= realizado + no realizado), win rate, retención mediana (si vende en <5 min es imposible copiarla con provecho), nº de tokens vs días, tamaños de compra, buy_rank en la evidencia.

CRITERIO Nº1 — GANANCIAS ABSOLUTAS: el objetivo del sistema son billeteras SÚPER RENTABLES, que ganan MUCHO dinero. El factor más importante es el pnl_neto_sol ABSOLUTO en SOL. Una billetera con +80 SOL netos vale mucho más que una con ROI% altísimo pero solo +1 SOL de ganancia absoluta (calderilla). Prioriza las ganancias absolutas grandes. Comprar temprano (buy_rank bajo) es un PLUS que refuerza, no el criterio principal: una early buyer que apenas gana NO vale; una que gana mucho SÍ, entre temprano o no.

IMPORTANTE sobre el PnL: juzga por el PnL NETO, no solo el realizado. Una billetera puede tener pnl_realizado_sol negativo porque AÚN NO ha vendido, mientras acumula posiciones ganadoras (pnl_no_realizado_sol alto). NO la castigues por acumular: si pnl_neto_sol es claramente positivo, cuenta a su favor. El realizado negativo solo es mala señal si el neto también lo es. Si el pnl_neto_sol es negativo o insignificante, la billetera NO vale aunque haya comprado muy temprano.

MÉTRICAS QUANT (si están presentes): profit_factor > 1.5, expectancy positiva, sharpe alto y max_drawdown contenido indican una operativa robusta y no un golpe de suerte. Un roi_max enorme con roi_median bajo y profit_factor ~1 es un "one-hit-wonder": NO es inteligente aunque el ROI promedio luzca bien.

CAMBIO DE COMPORTAMIENTO: si el patrón reciente contradice al histórico (p.ej. antes aguantaba horas y ahora hace flips de segundos, o cambió radicalmente de tamaños), menciónalo explícitamente en "razon".

Responde SOLO con JSON válido, sin markdown ni texto extra:
{{"clasificacion": "...", "seguir": true/false, "confianza": 0-100, "razon": "máximo 2 frases en español"}}"""


def _ensure_columns(conn):
    for col, typ in [("ai_class", "TEXT"), ("ai_follow", "INTEGER"),
                     ("ai_reason", "TEXT"), ("alias", "TEXT"),
                     ("pnl_30d", "REAL"), ("pnl_total", "REAL"),
                     ("pnl_unreal", "REAL"), ("pnl_net", "REAL"),
                     ("pnl_updated", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()


def _resumir_perfil(p: dict) -> str:
    import time as _t
    days_ago = ((_t.time() - p["last_tx_ts"]) / 86400) if p["last_tx_ts"] else None
    tokens = sorted(p["tokens"].items(), key=lambda x: x[1]["pnl_sol"],
                    reverse=True)
    top = [f"{m[:8]}: {i['pnl_sol']:+.2f} SOL ({i['buys']}c/{i['sells']}v)"
           for m, i in tokens[:8]]
    bottom = [f"{m[:8]}: {i['pnl_sol']:+.2f} SOL ({i['buys']}c/{i['sells']}v)"
              for m, i in tokens[-4:] if i["pnl_sol"] < 0]
    return json.dumps({
        "txs_muestreadas": p["tx_sampled"],
        "ultima_tx_hace_dias": round(days_ago, 1) if days_ago is not None else None,
        "txs_ultimos_7d": p["tx_7d"],
        "tokens_operados": len(p["tokens"]),
        "pnl_realizado_sol": round(p["pnl_total_sol"], 2),
        "pnl_no_realizado_sol": round(p.get("unrealized_sol", 0.0), 2),
        "pnl_neto_sol": round(p.get("net_pnl_sol", p["pnl_total_sol"]), 2),
        "tokens_en_cartera": p.get("held_tokens", 0),
        "pnl_30d_sol": round(p.get("pnl_30d_sol", 0.0), 2),
        "win_rate_pct": p.get("win_rate_pct"),
        "retencion_mediana_min": p.get("hold_median_min"),
        "posible_bot_por_frecuencia": p["possible_bot"],
        "flips_menos_1min_pct": p.get("flips_1min_pct"),
        "horas_del_dia_activas_de_24": p.get("active_hours_24"),
        "compras_tamano_identico_pct": p.get("uniform_buys_pct"),
        "tokens_estilo_market_maker": p.get("mm_tokens"),
        "metricas_quant": p.get("metrics") or {},
        "mejores": top, "peores": bottom,
    }, ensure_ascii=False)


def _call_claude(prompt: str, model: str) -> dict | None:
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        try:
            v = json.loads(text)
        except json.JSONDecodeError:
            # No tirar una llamada pagada por formato imperfecto: extraer
            # el primer bloque {...} del texto.
            import re as _re
            m = _re.search(r"\{.*\}", text, flags=_re.S)
            if not m:
                raise
            v = json.loads(m.group(0))
        if v.get("clasificacion") and isinstance(v.get("seguir"), bool):
            return v
    except Exception as e:
        print(f"  · Error IA ({model}): {e}")
    return None


def ai_verdict(profile: dict, evidence_lines: list[str],
               track_record: dict | None = None,
               avoid_aliases: list[str] | None = None) -> dict | None:
    """
    Veredicto en dos niveles: Haiku primero; si su confianza es baja,
    se consulta al modelo potente y prevalece su respuesta.
    """
    if not ANTHROPIC_API_KEY:
        return None
    prompt = PROMPT.format(
        perfil=_resumir_perfil(profile),
        track=json.dumps(track_record, ensure_ascii=False)
        if track_record else "(sin señales medidas todavía)",
        evidencia="\n".join(evidence_lines) or "(sin datos)",
        alias_evitar=", ".join(avoid_aliases) if avoid_aliases else "(ninguno aún)")

    v = _call_claude(prompt, MODEL_FAST)
    if v is None:
        return None
    try:
        conf = float(v.get("confianza", 0))
    except (TypeError, ValueError):
        conf = 0
    _escalate = os.getenv("AI_ESCALATE", "0") == "1"
    if _escalate and conf < CONF_ESCALATE:
        print(f"  · Confianza {conf:.0f}% < {CONF_ESCALATE}: "
              f"escalando a {MODEL_SMART}")
        v2 = _call_claude(prompt, MODEL_SMART)
        if v2:
            v2["modelo"] = MODEL_SMART
            return v2
    v["modelo"] = MODEL_FAST
    return v


def _hard_bot_reason(p: dict) -> str | None:
    """Filtro duro pre-IA: descarta casos flagrantes de bot/MEV/MM."""
    if p["possible_bot"]:
        return "frecuencia de transacciones inhumana"
    flips = p.get("flips_1min_pct")
    if flips is not None and flips >= 50 and p.get("closed_positions", 0) >= 4:
        return f"MEV/sniper bot: {flips}% de sus posiciones cierran en <1 min"
    if p.get("active_hours_24", 0) >= 22 and p["tx_sampled"] >= 200:
        return "activa las 24 horas del día sin pausas humanas"
    uni = p.get("uniform_buys_pct")
    if uni is not None and uni >= 80:
        return f"compras de tamaño idéntico ({uni}%): patrón de bot"
    mm = p.get("mm_tokens", 0)
    if mm >= 3:
        return f"market maker: {mm} tokens operados con posición neta ~0"
    return None


def _bump(conn, key: str, n: int = 1):
    """Contador persistente del embudo (métrica: ⭐ por cada 100 perfiladas)."""
    try:
        from db import get_setting, set_setting
        set_setting(conn, key,
                    int(float(get_setting(conn, key, "0") or 0)) + n)
    except Exception:
        pass


def evaluate_tracked(conn) -> int:
    """
    Perfila y clasifica las billeteras ⭐ sin veredicto, sin alias, o con
    veredicto caducado (>REEVAL_DAYS días). Guarda alias + PnL y descarta
    las que la IA rechaza. Devuelve cuántas evaluó.
    """
    _ensure_columns(conn)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=REEVAL_DAYS)).isoformat(timespec="seconds")
    try:
        import config as _cfg
        _lim = int(getattr(_cfg, "MAX_EVAL_PER_CYCLE", 20))
        _min = int(getattr(_cfg, "MIN_WINNING_TOKENS", 1))
    except Exception:
        _lim, _min = 20, 1
    # Candidatas a perfilar: las mejores por score (temprano + capital) que
    # aún no tienen veredicto o cuyo veredicto caducó. NO requiere ser ⭐:
    # la IA decidirá si merecen la estrella según su PnL. Así se perfilan en
    # silencio y solo las aprobadas empiezan a alertar.
    # Anti-sniper (embudo v4): solo se PERFILAN wallets con al menos una
    # compra FUERA de la zona de snipers (rank ≥ START, delay ≥ MIN_DELAY,
    # tamaño ≥ MIN_BUY_SOL). Los snipers/devs siguen en el grafo
    # (appearances) para clusters y afinidad, pero no gastan perfil.
    try:
        _r0 = int(getattr(_cfg, "BUYER_START_RANK", 30))
        _r1 = int(getattr(_cfg, "BUYER_END_RANK", 600))
        _d0 = int(getattr(_cfg, "MIN_BUY_DELAY_SEC", 60))
        _s0 = float(getattr(_cfg, "MIN_BUY_SOL", 1.0))
    except Exception:
        _r0, _r1, _d0, _s0 = 30, 600, 60, 1.0
    rows = conn.execute(
        """SELECT address FROM wallets w
           WHERE COALESCE(is_bot,0)=0 AND winning_tokens_count >= ?
             AND (ai_class IS NULL OR pnl_updated IS NULL OR pnl_updated < ?)
             AND EXISTS (SELECT 1 FROM appearances a
                         WHERE a.wallet = w.address
                           AND a.buy_rank >= ? AND a.buy_rank <= ?
                           AND COALESCE(a.delay_s, 999999) >= ?
                           AND COALESCE(a.buy_sol, 0) >= ?)
           ORDER BY score DESC
           LIMIT ?""",
        (_min, cutoff, _r0, _r1, _d0, _s0, _lim)).fetchall()
    if not rows:
        return 0

    try:
        from signal_tracker import wallet_track_record
    except Exception:
        wallet_track_record = None

    # Apodos ya usados → la IA debe evitarlos; aquí garantizamos unicidad
    try:
        owner = {r["alias"]: r["address"] for r in conn.execute(
            "SELECT alias, address FROM wallets WHERE alias IS NOT NULL"
        ).fetchall()}
    except Exception:
        owner = {}

    evaluated = 0
    for row in rows:
        addr = row["address"]
        print(f"\n🧠 IA evaluando {addr[:16]}…")
        profile = profile_wallet(addr)
        if not profile["tx_sampled"]:
            print("  · Sin datos; se deja pendiente para el próximo ciclo")
            continue

        # Filtro duro: bots/MEV/MM flagrantes se descartan sin gastar IA
        razon_bot = _hard_bot_reason(profile)
        if razon_bot:
            conn.execute(
                """UPDATE wallets SET is_bot=1, is_tracked=0, ai_class='bot',
                   ai_follow=0, ai_reason=?, alias=COALESCE(alias,'Bot Descartado'),
                   grade='Descartada',
                   pnl_30d=?, pnl_total=?, pnl_unreal=?, pnl_net=?, pnl_updated=?
                   WHERE address=?""",
                (f"Descarte automático: {razon_bot}",
                 round(profile.get("pnl_30d_sol", 0.0), 2),
                 round(profile.get("pnl_total_sol", 0.0), 2),
                 round(profile.get("unrealized_sol", 0.0), 2),
                 round(profile.get("net_pnl_sol", profile.get("pnl_total_sol", 0.0)), 2),
                 now_iso(), addr))
            conn.commit()
            evaluated += 1
            _bump(conn, "funnel_profiled")
            print(f"  🤖 Descartada sin IA: {razon_bot}")
            continue

        ev = conn.execute(
            "SELECT reason FROM appearances WHERE wallet=? LIMIT 6",
            (addr,)).fetchall()
        track = wallet_track_record(conn, addr) if wallet_track_record else None
        avoid = sorted(a for a, o in owner.items() if o != addr)
        # Presupuesto de IA: si se agotó, verdict=None → respaldo grading.
        try:
            from ai_budget import can_call, record_call
            if can_call(conn):
                verdict = ai_verdict(profile, [e["reason"] for e in ev],
                                     track, avoid_aliases=avoid)
                record_call(conn)
            else:
                verdict = None
                print("  💤 Presupuesto de IA agotado → clasificación por grading")
        except Exception:
            verdict = ai_verdict(profile, [e["reason"] for e in ev], track,
                                 avoid_aliases=avoid)
        if not verdict:
            # Sin IA (sin ANTHROPIC_API_KEY o sin créditos): RESPALDO por
            # grading — la rentabilidad decide, sin gastar IA. El bot sigue
            # confirmando billeteras aunque la API esté caída.
            try:
                from grading import grade_wallet
                from influence import influence as _inf
                gg = grade_wallet(profile, _inf(addr))
                follow = gg["tier"] in ("Elite", "Seguimiento")
                verdict = {
                    "clasificacion": "trader" if follow else "indeterminado",
                    "seguir": follow,
                    "confianza": 60,
                    "alias": None,
                    "razon": f"[sin IA] {gg['tier']}: "
                             + "; ".join(gg["reasons"][:2]),
                    "modelo": "grading",
                }
                print(f"  ⚙️ Sin IA → grading: {gg['tier']} "
                      f"({'seguir' if follow else 'descartar'})")
            except Exception as _e:
                print(f"  · Respaldo grading falló: {_e}")
                continue

        try:
            from wallet_score import compute_score
            wscore = compute_score(profile, track)["score"]
        except Exception:
            wscore = None

        # Grading en cascada: Consistency Score + nivel (Elite/…/Descartada)
        try:
            from grading import grade_wallet
            from influence import influence as _influence
            _grade = grade_wallet(profile, _influence(addr),
                                  verdict.get("clasificacion"))
        except Exception as _e:
            print(f"  · Grading no disponible: {_e}")
            _grade = None

        # Alias DETERMINISTA y único desde la dirección (código, no IA):
        # ni se repite ni gasta tokens; es estable en el tiempo.
        from aliases import make_alias
        alias = make_alias(addr)

        seguir = 1 if verdict["seguir"] else 0
        conn.execute(
            """UPDATE wallets SET ai_class=?, ai_follow=?, ai_reason=?,
               alias=COALESCE(?, alias),
               pnl_30d=?, pnl_total=?, pnl_unreal=?, pnl_net=?,
               grade=?, consistency=?,
               pnl_updated=?, wallet_score=?,
               is_tracked=?, is_bot=CASE WHEN ?='bot' THEN 1 ELSE is_bot END
               WHERE address=?""",
            (verdict["clasificacion"], seguir,
             verdict.get("razon", ""),
             alias,
             round(profile.get("pnl_30d_sol", 0.0), 2),
             round(profile.get("pnl_total_sol", 0.0), 2),
             round(profile.get("unrealized_sol", 0.0), 2),
             round(profile.get("net_pnl_sol", profile.get("pnl_total_sol", 0.0)), 2),
             (_grade or {}).get("tier"), (_grade or {}).get("consistency"),
             now_iso(), wscore,
             seguir, verdict["clasificacion"], addr),
        )
        conn.commit()
        evaluated += 1
        _bump(conn, "funnel_profiled")
        if seguir:
            _bump(conn, "funnel_promoted")
        icono = "✅" if seguir else "❌"
        print(f"  {icono} {verdict['clasificacion']} "
              f"«{alias or 'sin alias'}» "
              f"[{verdict.get('modelo', '?')}] "
              f"({verdict.get('confianza', '?')}%): {verdict.get('razon','')}")
    return evaluated
