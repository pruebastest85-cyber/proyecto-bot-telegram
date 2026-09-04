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
from influence import influence, graph, cache_lista

import os as _os

WINDOW_MIN = 20          # ventana para que lleguen los seguidores
EVAL_AFTER_MIN = 30      # a partir de aquí la predicción se evalúa sola


def _u(nombre, defecto):
    try:
        return int(_os.getenv(nombre, defecto))
    except (TypeError, ValueError):
        return int(defecto)


# ── (Ola 17-J, auditoria 6) UMBRALES CONFIGURABLES ────────────────────
# Estaban escritos a fuego, y medido sobre las 20.785 predicciones de la
# base del dueño: `confidence` maximo historico **75** contra un umbral
# de 85, y `meta_score` maximo **73** contra ALPHA_META=90. En 28 dias no
# ha habido UNA sola alerta, y no podia haberla: con el cluster frio el
# techo aritmetico de meta es 82 (dos componentes clavados, f_token=0.6 y
# f_cluster=0.3), o sea que `alpha` era matematicamente inalcanzable.
#
# Los defectos NO se tocan a proposito: bajarlos por mi cuenta llenaria
# el Telegram del dueño de alertas que nunca pidio. Lo que cambia es que
# ahora se pueden ajustar sin tocar codigo (variable de entorno o
# `settings`), y que `/metricas` DICE si el motor puede alertar o no.
DEFAULT_MIN_CONF = _u("PRED_MIN_CONF", 85)   # confianza para alertar
ALPHA_META = _u("PRED_ALPHA_META", 90)       # Meta Score para 🟢 Alpha
WATCH_META = _u("PRED_WATCH_META", 70)       # Meta Score para 🟡 Watchlist
MIN_LIQ_USD = _u("PRED_MIN_LIQ_USD", 20000)  # liquidez minima
MAX_RISK = _u("PRED_MAX_RISK", 70)           # riesgo maximo


def _umbrales(conn=None) -> dict:
    """Umbrales efectivos: `settings` manda sobre el entorno."""
    u = {"conf": DEFAULT_MIN_CONF, "alpha": ALPHA_META,
         "watch": WATCH_META, "liq": MIN_LIQ_USD, "risk": MAX_RISK}
    if conn is None:
        return u
    for clave, ajuste in (("conf", "pred_min_confidence"),
                          ("alpha", "pred_alpha_meta"),
                          ("watch", "pred_watch_meta"),
                          ("liq", "pred_min_liq_usd"),
                          ("risk", "pred_max_risk")):
        try:
            v = get_setting(conn, ajuste, None)
            if v is not None and str(v).strip() != "":
                u[clave] = int(float(v))
        except (TypeError, ValueError):
            pass
    return u


def _tier(conf: int, meta: int, umbral: int,
          liq=None, risk=None, u: dict | None = None) -> str:
    """🟢 alpha / 🟡 watchlist / 🔴 ignored. Filtros duros primero."""
    u = u or _umbrales()
    if liq is not None and (liq or 0) < u["liq"]:
        return "ignored"          # baja liquidez
    if risk is not None and (risk or 0) >= u["risk"]:
        return "ignored"          # riesgo elevado (rug/concentración)
    if meta >= u["alpha"] and conf >= umbral:
        return "alpha"
    if meta >= u["watch"] and conf >= 60:
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

def _candado_mint(mint: str):
    """Candado por token, el MISMO que serializa `positions` y la copia
    simulada en realtime. Se pide aqui —y no en el llamador— para poder
    soltarlo antes de enviar la alerta.

    Si por lo que sea no se puede importar, se sigue sin candado: perder
    una llegada de vez en cuando es malo, pero dejar de registrar
    llegadas del todo es peor.
    """
    try:
        from realtime import _lock_mint
        return _lock_mint(mint)
    except Exception as e:
        print(f"· Predicción: sin candado por token ({e})")
        import contextlib
        return contextlib.nullcontext()


def marca_medicion(conn) -> int:
    """La marca, SIN ponerla si no existe. Para los paneles.

    (Ola 18-H) `/metricas` y `/predicciones` solo leen; si llamaran a
    `medicion_desde` serian ellos quienes fijaran la marca —"la medición
    empieza cuando abriste el panel"— en vez de la primera compra que pasa
    por el motor. Aquí, si aún no hay marca, se devuelve 0 y los paneles
    miden como siempre.
    """
    try:
        v = get_setting(conn, "pred_medicion_desde", None)
        return int(float(v)) if v else 0
    except Exception:
        return 0


def medicion_desde(conn) -> int:
    """Desde cuándo valen las mediciones del motor predictivo.

    (Ola 18-G) Las 20.785 predicciones ya evaluadas se puntuaron con el
    denominador equivocado: se predecian seguidores del grafo entero y se
    confirmaban solo contra ⭐. 19.378 de ellas (93%) tienen acierto 0 sin
    que eso diga nada del lider. Si se siguieran contando, todos los
    lideres quedarian en factor 0,3 durante 30 dias MAS despues del
    arreglo, y el motor seguiria sin poder alertar por una division que ya
    no existe. La marca se pone la primera vez que corre el codigo nuevo:
    a partir de ahi, se mide de verdad.
    """
    try:
        v = get_setting(conn, "pred_medicion_desde", None)
        if v:
            return int(float(v))
    except Exception:
        # Incluye fallos de base (bloqueo de SQLite, Postgres caido): sin
        # la marca se mide como antes, que es peor pero no rompe nada.
        return 0
    ahora = int(time.time())
    try:
        from db import set_setting
        set_setting(conn, "pred_medicion_desde", str(ahora))
        print("🔮 Motor predictivo: la medición del acierto empieza de "
              "cero (las anteriores se calcularon con el denominador "
              "equivocado; ver Ola 18-G)")
        return ahora
    except Exception as e:
        # Si no se puede guardar, NO se devuelve `ahora`: eso dejaria la
        # ventana clavada en "desde este instante" en CADA llamada y la
        # salud del lider en n=0 para siempre. Mejor caer al
        # comportamiento de antes y volver a intentarlo luego.
        print(f"· No pude fijar pred_medicion_desde ({e}); mido como antes")
        return 0


def _leader_health(conn, leader: str) -> dict:
    """Precisión de las predicciones del líder, hasta 30 días atrás y
    nunca antes de que la medición empezara a ser correcta."""
    since = int(time.time()) - 30 * 86400
    # (Ola 18-G) El corte va sobre `created_ts`, NO sobre `evaluated_ts`.
    # Con `evaluated_ts` se colarian las predicciones que estaban ABIERTAS
    # en el momento del despliegue: se crearon con el denominador
    # equivocado y se cierran despues de la marca. Son ~15 (la ventana es
    # de 30 min y el ritmo, 31/h), pero si a UNA sola ⭐ le tocan 5 o mas
    # en mints distintos, arranca con acierto 0 y n>=5 → "en declive",
    # factor 0,3 — exactamente el pozo del que esta ola la saca.
    marca = medicion_desde(conn)
    rows = conn.execute(
        """SELECT outcome_pct FROM predictions
           WHERE leader=? AND status='evaluada'
             AND evaluated_ts>=? AND created_ts>=?""",
        (leader, since, marca)).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0, "accuracy": None, "estado": "sin historial", "factor": 0.6}
    acc = sum((r["outcome_pct"] or 0) for r in rows) / n
    if n < 5:
        # (Ola 8) Con menos de 5 evaluadas no se sentencia "en declive"
        # ni "caliente": muestra insuficiente, factor neutro.
        return {"n": n, "accuracy": round(acc),
                "estado": f"🟡 Muestra corta ({n})", "factor": 0.65}
    if acc >= 70:
        estado, factor = "🟢 Caliente", 1.0
    elif acc < 50:
        estado, factor = "🔴 En declive", 0.3
    else:
        estado, factor = "🟡 Normal", 0.65
    return {"n": n, "accuracy": round(acc), "estado": estado, "factor": factor}


def _num(v, defecto=0.0) -> float:
    """(Ola 17-E) Numero o el defecto. Los factores venian de dicts que
    hoy siempre traen numeros, pero un None colado (una rama nueva, un
    dato de la base) reventaba el calculo entero de la alerta."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(defecto)
    return x if x == x and x not in (float("inf"), float("-inf")) else float(defecto)


# (Ola 17-A) Valor neutro de la etapa 1 y cuántos puntos de los 100 de
# "Confianza" representa. Se nombra para poder decirlo en la alerta.
CONF_STAGE_NEUTRO = 0.5
CONF_PTS_NEUTROS = round(15 * CONF_STAGE_NEUTRO, 1)


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
    shared = [_num(f.get("shared"), 0) for f in followers]
    probs = [_num(f.get("prob"), 0) / 100 for f in followers]
    f_hist = min(1.0, (sum(shared) / len(shared)) / 8.0)      # ≥8 coincid. = tope
    f_stab = sum(probs) / len(probs)                           # prob media edges
    f_lead = (inf.get("leader_score") or 0) / 100
    f_liq = 1.0 if (liq or 0) >= 20000 else max(0.0, (liq or 0) / 20000)
    f_health = _num(health.get("factor"), 0.6)
    if arrived > 0:
        f_stage = min(1.0, arrived / max(1, len(followers)))   # etapas
    else:
        # Etapa 1 (sin confirmaciones aun): valor neutro. Con 0, el maximo
        # teorico era 85 y el umbral por defecto (85) hacia practicamente
        # imposible alertar en la etapa inicial.
        # (Ola 17-A) Se mantiene, pero la alerta ya DICE que estos puntos
        # no estan medidos: son 7,5 de los 100 de "Confianza".
        f_stage = CONF_STAGE_NEUTRO
    score = (25 * f_hist + 20 * f_stab + 15 * f_lead +
             10 * f_liq + 15 * f_health + 15 * f_stage)
    return round(min(100.0, score))


# Peso 10 × 0.6 = 6 puntos fijos del Meta Score. Ver comentario abajo.
META_TOKEN_NEUTRO = 0.6
META_PTS_NEUTROS = round(10 * META_TOKEN_NEUTRO)


def meta_score(inf: dict, cluster: dict | None, health: dict,
               followers: list, liq, risk_pct) -> int:
    """
    Meta Score 0-100 de la señal (pesos del diseño):
      Leader 20 · Cluster 20 · Historial líder 15 · Propagación 20 ·
      Liquidez 10 · Riesgo 5 · (Historial token 10 → SIEMPRE neutro:
      no hay ninguna rama que lo calcule, son 6 puntos fijos).
    """
    f_lead = (inf.get("leader_score") or 0) / 100
    if cluster:
        f_cluster = min(1.0, (cluster.get("shared_tokens", 0) / 6.0) *
                        (cluster.get("size", 0) / 5.0) + 0.2)
        f_cluster = min(1.0, f_cluster)
    else:
        f_cluster = 0.3
    f_hist = _num(health.get("factor"), 0.6)
    probs = [_num(f.get("prob"), 0) / 100 for f in followers] or [0]
    f_prop = sum(probs) / len(probs)
    f_liq = 1.0 if (liq or 0) >= 20000 else max(0.0, (liq or 0) / 20000)
    # (Ola 17-A) Este componente NUNCA se ha calculado: no existe rama que
    # lo mida. Son 6 de los 100 puntos que siempre valen lo mismo. Se deja
    # el neutro (quitarlo moveria todos los umbrales ya calibrados), pero
    # la alerta ahora lo DICE en vez de presentar 100 puntos como medidos.
    f_token = META_TOKEN_NEUTRO
    f_risk = 1.0 - min(1.0, (risk_pct or 0) / 100)   # menos riesgo = mejor
    score = (20 * f_lead + 20 * f_cluster + 15 * f_hist + 20 * f_prop +
             10 * f_liq + 10 * f_token + 5 * f_risk)
    return round(min(100.0, score))


# ─────────────────────────── MOTOR ──────────────────────────────────

def _send(text: str) -> bool:
    """(19-D) Devuelve si Telegram ACEPTO el mensaje.

    Antes no devolvia nada y `on_buy` marcaba `alerted_stage` ANTES de
    llamar aqui: una alerta rechazada (400 por Markdown roto, 429 por
    ritmo) quedaba apuntada como enviada. Doble daño: el dueño no recibia
    la ALPHA, y `/metricas` contaba esa fila como alertada al calcular la
    tasa de falsos positivos — o sea, una metrica construida sobre
    alertas que nunca salieron."""
    try:
        from realtime import tg_send
        return bool(tg_send(text))
    except Exception as e:
        print(f"· Predicción: no pude enviar alerta: {e}")
        return False


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
    # (Ola 18-C) `construir=False`: esto corre en el hilo de ingesta y
    # solo se usa para poner un alias bonito en el mensaje. Si el cache
    # esta frio, `.get(...)` cae al alias corto y la alerta sale igual.
    alias = graph(construir=False)["wallets"].get(
        pred_row["leader"], {}).get("alias", pred_row["leader"][:6])
    badge = _TIER_BADGE.get(pred_row["tier"] or "", "")
    lines = [f"{badge}",
             f"🔮 *SEÑAL PREDICTIVA — {nivel}*",
             f"Líder: *{alias}* · Token: `{sym}`",
             f"Confianza: *{conf}%* · Meta Score: *{meta}/100*"]
    # (Ola 17-A) Decir qué parte de esos números NO está medida.
    _neutros = [f"{META_PTS_NEUTROS} pts del Meta Score (sin histórico "
                f"propio del token: nunca se calcula)"]
    if token_ctx.get("_cluster_sabido") is False:
        _neutros.append("6 pts de Cluster (aún sin calcular: el grafo de "
                        "co-compra no estaba listo)")
    # (Ola 18-C) Lo mismo para el grafo de influencia: si no estaba en
    # caché, los puntos de liderazgo valen 0 por falta de dato, no porque
    # se haya medido que el líder no lidera.
    if token_ctx.get("_influencia_sabida") is False:
        _neutros.append("15 pts de Liderazgo (aún sin calcular: el grafo "
                        "de influencia no estaba listo)")
    if stage <= 1:
        _neutros.append(f"{CONF_PTS_NEUTROS} pts de Confianza (etapa 1: "
                        f"aún no ha confirmado ningún seguidor)")
    lines.append("_⚪ Neutros por falta de datos: " + " · ".join(_neutros) + "._")
    # (Ola 18-G) Seguidores que el grafo ve pero el bot no puede observar.
    _oc = token_ctx.get("_seguidores_ocultos") or 0
    if _oc:
        lines.append(f"_👁 Otras {_oc} billeteras suelen entrar detrás de "
                     f"este líder, pero no están vigiladas: no puedo "
                     f"confirmarlas, así que no cuentan en la nota._")
    if health.get("accuracy") is not None:
        lines.append(f"Salud del líder: {health['estado']} "
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
    return _send("\n".join(lines))          # (19-D) propaga la entrega


def on_buy(conn, wallet: str, mint: str, ts: int, token_ctx: dict,
           vigiladas=None, es_estrella: bool = True):
    """Punto de entrada desde realtime cuando una billetera VIGILADA compra.

    (Ola 18-G) `vigiladas` es el conjunto de billeteras cuyas compras el
    bot puede VER, y `es_estrella` dice si esta en concreto es ⭐.

    POR QUE HACEN FALTA. El motor predecia seguidores sacados del grafo de
    co-compra —25.000 billeteras— pero solo registraba llegadas cuando el
    comprador era ⭐. Numerador y denominador de poblaciones distintas:
    medido sobre la base del dueño, de 22.862 seguidores predichos solo el
    **21,3%** son observables hoy, y **0%** eran ⭐. Resultado:
    `outcome_pct` = 0 en **19.378 de 20.785** predicciones (93%), todos los
    lideres al factor de salud 0,3, techo de meta 72 contra un umbral de
    90 y **cero alertas en 20.785 predicciones**. Los 28 dias sin alertas
    no eran calibracion: era una division mal planteada.

    Dos arreglos, los dos aqui:
      · solo se PREDICEN seguidores observables (los demas se cuentan y se
        dicen en la alerta, pero no entran en la nota);
      · las LLEGADAS se registran para cualquier vigilada, no solo ⭐.
        La puerta de "solo ⭐" se puso en la Ola 5 porque `on_buy`
        reconstruia el grafo caro dentro del hilo de ingesta; desde la
        Ola 18-C eso ya no pasa (`construir=False`), asi que el motivo de
        aquella puerta ya no existe.
    Abrir una prediccion NUEVA sigue siendo cosa de ⭐: son las que se
    copian.
    """
    try:
        evaluate_due(conn)          # cierra predicciones vencidas primero
    except Exception as e:
        print(f"· Predicción eval: {e}")

    # (Ola 17-J) Umbrales efectivos: `settings` manda sobre el entorno,
    # y el entorno sobre el defecto. Antes estaban a fuego en el modulo.
    _u_ef = _umbrales(conn)
    umbral = _u_ef["conf"]

    # ¿Hay ya una predicción abierta para este token?
    row = conn.execute(
        "SELECT * FROM predictions WHERE mint=? AND status='abierta' "
        "ORDER BY created_ts DESC LIMIT 1", (mint,)).fetchone()

    if row:
        # ── Registrar la llegada: leer-modificar-escribir bajo candado ──
        # (Ola 18-G) El candado va SOLO alrededor de esto, no del envio a
        # Telegram: `_alert_stage` hace red y puede tardar hasta ~30 s
        # (dos intentos de 15), y este es el MISMO candado por token que
        # serializa `positions` y la copia simulada. Con el envio dentro,
        # una alerta lenta retrasaba media minuto la copia de ese token en
        # otro hilo. Se prepara todo dentro, se envia fuera.
        aviso = None
        with _candado_mint(mint):
            # Se RELEE dentro del candado: la fila de fuera puede ser de
            # hace un instante y otro hilo haber sumado ya su llegada.
            row = conn.execute(
                "SELECT * FROM predictions WHERE id=?",
                (row["id"],)).fetchone()
            if not row or row["status"] != "abierta":
                return
            pred = json.loads(row["predicted"] or "[]")
            pred_w = {p["wallet"] for p in pred}
            if wallet in pred_w and wallet != row["leader"]:
                arrived = set(json.loads(row["arrived"] or "[]"))
                if wallet not in arrived:
                    first = None
                    if not arrived:      # primer seguidor en llegar
                        first = max(0, int(ts) - int(row["created_ts"] or ts))
                    arrived.add(wallet)
                    # (Ola 18-C) camino caliente: solo cache, nunca
                    # construye. Si el grafo aún no está, `inf` queda
                    # vacío y `confidence_score` no suma los puntos de
                    # liderazgo: se avisa en el mensaje en vez de darlo
                    # por medido.
                    inf = influence(row["leader"], construir=False) or {}
                    _inf_sabida = bool(inf) or cache_lista()
                    health = _leader_health(conn, row["leader"])
                    stage = 1 + len(arrived)
                    # (Ola 18-H) Si el grafo NO esta, no se recalcula.
                    #
                    # De todo lo que entra en `confidence_score`, lo unico
                    # que sale del grafo es la fuerza del lider: 15 de los
                    # 100 puntos. Con la cache fria `inf` queda vacio, ese
                    # factor cae a 0 y la confianza baja hasta 15 puntos
                    # de golpe — y hasta ahora esa cifra se ESCRIBIA en la
                    # fila, sin ninguna marca de que no se habia medido, y
                    # el `tier` degradado se llevaba por delante la alerta
                    # ALPHA. Comprobado ejecutandolo: la misma
                    # confirmacion daba 89/alpha con la cache caliente y
                    # 75/watchlist en frio.
                    #
                    # No es un caso raro: `_CACHE["g"]` arranca vacio,
                    # `warmup_job` no entra hasta los 40 s y `_build()`
                    # tarda ~37 s, o sea ~77 s ciegos DESPUES DE CADA
                    # REINICIO — y el supervisor reinicia en cada
                    # despliegue.
                    #
                    # La prediccion solo se pudo ABRIR con el grafo
                    # delante, asi que `confidence` y `tier` de la fila ya
                    # se midieron con el. Cuando el dato falta se conserva
                    # lo medido y se apunta la llegada igual: la confianza
                    # se queda corta durante ese minuto (no sube por esta
                    # confirmacion), que es el error seguro. Inventar el
                    # liderazgo a 0 era el error peligroso.
                    if _inf_sabida:
                        conf = confidence_score(inf, pred,
                                                token_ctx.get("liq"), health,
                                                arrived=len(arrived))
                        tier = _tier(conf, row["meta_score"] or 0, umbral,
                                     token_ctx.get("liq"),
                                     _risk_pct(token_ctx), u=_u_ef)
                        conn.execute(
                            "UPDATE predictions SET arrived=?, stage=?, "
                            "confidence=?, tier=?, "
                            "first_confirm_s=COALESCE(first_confirm_s,?) "
                            "WHERE id=?",
                            (json.dumps(sorted(arrived)), stage, conf, tier,
                             first, row["id"]))
                    else:
                        conf = row["confidence"] or 0
                        tier = row["tier"]
                        print(f"· Predicción {row['id']}: grafo aún sin "
                              f"construir; se apunta la llegada y se "
                              f"conserva la confianza medida ({conf}).")
                        conn.execute(
                            "UPDATE predictions SET arrived=?, stage=?, "
                            "first_confirm_s=COALESCE(first_confirm_s,?) "
                            "WHERE id=?",
                            (json.dumps(sorted(arrived)), stage,
                             first, row["id"]))
                    conn.commit()
                    if (_should_push(tier, conn)
                            and stage > (row["alerted_stage"] or 0)):
                        # (19-D) La marca se pone DESPUES del envio, ver
                        # el aviso mas abajo; aqui solo se prepara.
                        fresh = conn.execute(
                            "SELECT * FROM predictions WHERE id=?",
                            (row["id"],)).fetchone()
                        _ctx_al = dict(token_ctx)
                        # (Ola 18-H) Si la confianza que se envia es la
                        # que se midio CON el grafo (porque ahora, con la
                        # cache fria, no se recalcula), entonces los
                        # puntos de liderazgo SI estan dentro y la nota de
                        # "15 pts aun sin calcular" mentiria al reves.
                        _ctx_al["_influencia_sabida"] = True
                        # (Ola 18-G) La nota de "hay N seguidores que no
                        # puedo ver" tambien vale en las confirmaciones;
                        # se recalcula desde la propia prediccion, que ya
                        # solo guarda observables.
                        # (Ola 18-H) Se cuentan los mismos que en la
                        # apertura: los que superan el umbral de fuerza.
                        # Con TODOS los del grafo, el numero mezclaba "no
                        # llega al umbral" con "no la puedo ver" y salia
                        # mas alto de lo que es.
                        _ocultos_conf = 0
                        try:
                            _f60 = [f for f in
                                    ((inf or {}).get("followers") or [])
                                    if _num(f.get("prob"), 0) >= 60]
                            _ocultos_conf = max(0, len(_f60) - len(pred))
                        except Exception:
                            pass
                        _ctx_al["_seguidores_ocultos"] = _ocultos_conf
                        aviso = (fresh, inf, conf, row["meta_score"], pred,
                                 health, _ctx_al)
        if aviso:
            # Fuera del candado: la red no bloquea a nadie.
            # (19-D) Y la marca del escalón se pone DESPUÉS, solo si
            # Telegram aceptó. Antes se escribía `alerted_stage` con su
            # commit ANTES de llegar aquí: como la condición de aviso es
            # `stage > alerted_stage`, un rechazo (400 por el símbolo del
            # token, 429 por ritmo) mataba esa etapa para siempre — y
            # además `/metricas` la contaba como alertada al calcular la
            # tasa de falsos positivos, o sea una métrica construida
            # sobre alertas que nunca salieron.
            _fresh = aviso[0]
            if _alert_stage(*aviso) and _fresh is not None:
                try:
                    conn.execute(
                        "UPDATE predictions SET alerted_stage=? "
                        "WHERE id=? AND COALESCE(alerted_stage,0) < ?",
                        (_fresh["stage"], _fresh["id"], _fresh["stage"]))
                    conn.commit()
                except Exception as e:
                    print(f"· Predicción: no pude marcar la etapa "
                          f"entregada ({e})")
            elif _fresh is not None:
                print(f"· Predicción {_fresh['id']}: la confirmación de "
                      f"etapa {_fresh['stage']} no se entregó; no la "
                      f"marco, se reintenta en la próxima llegada")
        return

    # No hay predicción abierta: ¿este comprador es LÍDER con seguidores?
    # Abrir una predicción es cosa de ⭐: son las que se copian. Las demás
    # vigiladas llegan hasta aquí solo para poder CONFIRMAR predicciones
    # ajenas, que es lo que arriba se acaba de hacer.
    if not es_estrella:
        return
    # (Ola 18-C) camino caliente: solo cache, nunca construye.
    inf = influence(wallet, construir=False)
    if not inf or not inf.get("followers"):
        # None significa dos cosas distintas: "esta billetera no lidera a
        # nadie" (dato) o "todavia no tengo el grafo" (falta de dato). En
        # el segundo caso la predicción se PIERDE, no se retrasa: on_buy
        # solo corre con la compra. Se deja constancia en el log para que
        # la ventana fría del arranque sea visible y medible.
        if inf is None and not cache_lista():
            print(f"· Predicción: grafo de influencia aún sin construir; "
                  f"no evalúo la compra de {wallet[:8]}")
        return
    followers = [f for f in inf["followers"] if f["prob"] >= 60]
    # (Ola 18-G) Solo los que el bot puede VER operar. Predecir a alguien
    # cuyas compras no llegan nunca no es una prediccion: es un numero que
    # solo puede fallar, y ademas arrastra hacia abajo la nota del lider.
    # Sin `vigiladas` (llamadas viejas o pruebas) no se filtra nada.
    ocultos = 0
    # `is not None`, no `if vigiladas`: un conjunto vacio significa "no
    # puedo ver a nadie", y entonces lo correcto es no predecir — no
    # desactivar el filtro en silencio. (Hoy `realtime` ya corta antes si
    # no hay vigiladas; esto es para cualquier otro llamador.)
    if vigiladas is not None:
        _vis = [f for f in followers if f["wallet"] in vigiladas]
        ocultos = len(followers) - len(_vis)
        followers = _vis
    if len(followers) < 2:
        return

    health = _leader_health(conn, wallet)
    from clusters import cluster_for
    cluster = None
    cluster_sabido = False
    try:
        # (Ola 17-D) `construir=False`: esto corre en el hilo del
        # webhook, con una señal esperando. Si la cache esta fria se
        # devuelve None y la proxima señal ya la tendra; antes esta
        # linea reconstruia el grafo de co-compra ENTERO en cada compra
        # de una ⭐.
        cluster = cluster_for(wallet, construir=False)
        # (Ola 17-E) None puede significar "no tiene cluster" o "aun no lo
        # se" (cache fria tras un reinicio, o enfriamiento tras fallo).
        # Hay que distinguirlo para no dar por medido un neutro.
        from clusters import cache_lista as _cl
        cluster_sabido = _cl()
    except Exception as e:
        # (Ola 17-A) Antes era `pass`: si esto fallaba, f_cluster caia al
        # neutro 0.3 (6 de 20 puntos) y se presentaba como medido, sin
        # dejar ni una linea en el log. Ahora se ve.
        print(f"· Predicción: no pude leer el cluster de {wallet[:8]}: {e}")
        try:
            from errores import record as _rec_err
            _rec_err("predictions.cluster", e)
        except Exception:
            pass
    conf = confidence_score(inf, followers, token_ctx.get("liq"), health)
    meta = meta_score(inf, cluster, health, followers,
                      token_ctx.get("liq"), _risk_pct(token_ctx))
    token_ctx = dict(token_ctx)
    token_ctx["_cluster_sabido"] = cluster_sabido
    # (Ola 18-G) Los seguidores que el grafo ve pero el bot NO puede
    # observar: no entran en la nota, pero se dicen, porque son
    # informacion real sobre el token.
    token_ctx["_seguidores_ocultos"] = ocultos
    # Aquí el grafo de influencia SÍ estaba: si no, `inf` habría sido None
    # y se habría vuelto antes. Se deja explícito para que no dependa de
    # esa cadena de razonamiento si alguien toca la guarda de arriba.
    token_ctx["_influencia_sabida"] = True

    tier = _tier(conf, meta, umbral, token_ctx.get("liq"),
                 _risk_pct(token_ctx), u=_u_ef)
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
        # (19-D) ENVIAR PRIMERO, MARCAR DESPUES. Antes se escribia
        # `alerted_stage=1` y se commiteaba ANTES de enviar; si Telegram
        # rechazaba, la prediccion quedaba "alertada" sin que el dueño
        # hubiera visto nada, y ademas contaminaba la tasa de falsos
        # positivos de /metricas.
        row = conn.execute(
            "SELECT * FROM predictions WHERE leader=? AND mint=? "
            "AND status='abierta'", (wallet, mint)).fetchone()
        if _alert_stage(row, inf, conf, meta, followers, health, token_ctx):
            conn.execute(
                "UPDATE predictions SET alerted_stage=1 "
                "WHERE leader=? AND mint=? AND status='abierta'",
                (wallet, mint))
            conn.commit()
        else:
            print(f"· Predicción de {mint[:8]}…: la ALPHA no se entregó; "
                  f"no la marco como alertada")


def evaluate_due(conn):
    """Cierra y evalúa las predicciones cuya ventana ya venció."""
    cutoff = int(time.time()) - EVAL_AFTER_MIN * 60
    rows = conn.execute(
        "SELECT * FROM predictions WHERE status='abierta' AND created_ts<=?",
        (cutoff,)).fetchall()
    for r in rows:
        pred = json.loads(r["predicted"] or "[]")
        arrived = set(json.loads(r["arrived"] or "[]"))
        # (Ola 18-G) El denominador son los seguidores que el bot podia
        # VER. Desde esta ola `predicted` ya solo trae observables, pero
        # las 20.785 filas viejas traen el grafo entero: para esas se
        # sigue calculando como siempre (no se puede reconstruir a
        # posteriori quien estaba vigilado entonces) y por eso la salud
        # del lider las ignora — ver `medicion_desde`.
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
            # (19-AA, auditoria M4) Mismo criterio que signal_tracker
            # (Ola 8): DexScreener con 200 y SIN pares = el token dejo
            # de cotizar → -100 (perdida total). Antes se pedia el
            # precio a `analyze_token` (3 llamadas: DexScreener + 2 de
            # RugCheck) y un token muerto no tenia precio, asi que la
            # fila quedaba NULL PARA SIEMPRE y se reintentaba cada 10
            # min: 31 filas en la base del dueño llevaban 3 semanas
            # asi, y `AVG(token_chg_pct)` de /metricas ignoraba justo
            # los rugs. Un fallo de red (muerto=False) sigue dejando
            # NULL para reintentar.
            from signal_tracker import _price_mc_ex
            now_px, _mc, muerto, _liq = _price_mc_ex(r["mint"])
            if now_px and r["price0"]:
                chg = round(100 * (now_px - r["price0"]) / r["price0"])
            elif muerto:
                chg = -100
            else:
                continue
            conn.execute(
                "UPDATE predictions SET token_chg_pct=? WHERE id=?",
                (chg, r["id"]))
        except Exception as e:
            print(f"· Predicciones: no pude medir el token "
                  f"{str(r['mint'])[:8]} ({e}); se reintenta luego")
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
    return (f"Salud del líder: {h['estado']} · "
            f"{h['accuracy']}% de acierto en {h['n']} predicciones")


def predictions_text(limit: int = 10) -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY created_ts DESC LIMIT ?",
            (limit,)).fetchall()
        # (Ola 18-H) La precision global tambien se corta por la marca.
        # La Ola 18-G movio la linea base en el MOTOR (`_leader_health`)
        # pero no en los PANELES, asi que estos seguian promediando las
        # 20.785 filas que la propia 18-G declaro incalculables: el panel
        # podia decir "18% de precision" mientras el motor trabajaba con
        # el 80% real de las nuevas.
        _marca = marca_medicion(conn)
        ev = conn.execute(
            "SELECT COUNT(*) n, AVG(outcome_pct) a FROM predictions "
            "WHERE status='evaluada' AND created_ts>=?",
            (_marca,)).fetchone()
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
        # (Ola 18-H) Todo lo que es CALIDAD (acierto, falsos positivos,
        # ranking de lideres, calibracion) se mide desde la marca de la
        # Ola 18-G; lo que es VOLUMEN (total, etapas, tiers) sigue
        # contando la tabla entera, porque ahi el historico no estorba.
        marca = marca_medicion(conn)
        ev = conn.execute(
            "SELECT COUNT(*) n, AVG(outcome_pct) acc, AVG(first_confirm_s) t, "
            "AVG(token_chg_pct) chg FROM predictions "
            "WHERE status='evaluada' AND created_ts>=?",
            (marca,)).fetchone()
        fp = conn.execute(
            "SELECT COUNT(*) c FROM predictions "
            "WHERE status='evaluada' AND alerted_stage>0 AND outcome_pct=0 "
            "AND created_ts>=?", (marca,)).fetchone()["c"]
        # (Ola 17-A) El denominador contaba TODAS las alertadas, incluidas
        # las que aun no se han evaluado, mientras el numerador solo cuenta
        # evaluadas: la tasa salia sistematicamente mejor de lo real
        # ("3/50" cuando quiza solo 6 de esas 50 estan evaluadas).
        # Numerador y denominador de la MISMA poblacion: `fp` ya va
        # cortado por la marca, asi que estos dos tambien.
        alerted = conn.execute(
            "SELECT COUNT(*) c FROM predictions "
            "WHERE alerted_stage>0 AND status='evaluada' AND created_ts>=?",
            (marca,)).fetchone()["c"]
        alerted_abiertas = conn.execute(
            "SELECT COUNT(*) c FROM predictions "
            "WHERE alerted_stage>0 AND status<>'evaluada' AND created_ts>=?",
            (marca,)).fetchone()["c"]
        tiers = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) c FROM predictions GROUP BY tier").fetchall()}
        # (Ola 8, 21/8) "Mas fiables" exigia n>=1: un lider con 100% en
        # UNA prediccion encabezaba por delante de 90% en veinte. Minimo
        # 5 evaluadas para entrar al ranking.
        leaders = conn.execute(
            """SELECT leader, COUNT(*) n, AVG(outcome_pct) acc
               FROM predictions WHERE status='evaluada' AND created_ts>=?
               GROUP BY leader HAVING COUNT(*)>=5
               ORDER BY acc DESC, n DESC LIMIT 5""", (marca,)
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

    # (Ola 17-J, auditoria 6) AUTODIAGNOSTICO. El motor llevaba 28 dias y
    # 20.785 filas sin emitir una sola alerta, y no habia forma de saberlo
    # mirando el panel. Ahora se compara lo que el sistema PRODUCE de
    # verdad con los umbrales que hacen falta para alertar.
    _diag = []
    try:
        _c2 = get_conn()
        try:
            _u = _umbrales(_c2)
            # (Ola 18-H) La calibracion mide lo que el motor PRODUCE
            # hoy. Con la tabla entera, las 20.785 filas viejas —hechas
            # con el denominador roto de antes de la Ola 18-G— marcaban un
            # techo que ya no es el de ahora, y el panel podia seguir
            # diciendo "el motor NO puede alertar" mientras alertaba.
            _mx = _c2.execute(
                "SELECT MAX(confidence) c, MAX(meta_score) m, "
                "COUNT(*) n, SUM(CASE WHEN alerted_stage>0 THEN 1 ELSE 0 END) a "
                "FROM predictions WHERE created_ts>=?", (marca,)).fetchone()
        finally:
            _c2.close()
        _mc, _mm = _mx["c"] or 0, _mx["m"] or 0
        _alertadas = _mx["a"] or 0
        if _mx["n"]:
            _diag.append(
                f"\n🔎 *Calibración* — máximos alcanzados en "
                f"{_mx['n']:,} predicciones: confianza *{_mc}* · "
                f"Meta *{_mm}*")
            _diag.append(
                f"   Umbrales para alertar: confianza ≥{_u['conf']} "
                f"Y Meta ≥{_u['alpha']}")
            if _alertadas == 0 and (_mc < _u["conf"] or _mm < _u["alpha"]):
                _diag.append(
                    "   ⚠️ *Con estos umbrales el motor NO puede alertar*: "
                    "nunca ha llegado.\n"
                    "   Se ajustan con estas variables de entorno (en "
                    "`bot_local.env`, reiniciando el bot):\n"
                    f"   `PRED_ALPHA_META` (ahora {_u['alpha']}) · "
                    f"`PRED_MIN_CONF` (ahora {_u['conf']}) · "
                    f"`PRED_WATCH_META` (ahora {_u['watch']})")
            elif _alertadas == 0:
                _diag.append("   Aún sin alertas emitidas, pero los "
                             "umbrales son alcanzables.")
    except Exception as _e:
        _diag = [f"\n🔎 Calibración: no se pudo comprobar ({_e})"]

    out = ["📊 *Panel del motor predictivo*\n",
           f"Creadas: {emit_1d} en 24h · {emit_7d} en 7d · {total} total "
           f"(incluye las que no llegaron a alertarse)",
           f"Alcanzan Nivel 2: {pct(n2, total)} · Nivel 3: {pct(n3, total)}"]
    if marca:
        # (Ola 18-H) Que se vea POR QUE el numero de evaluadas es pequeño:
        # las de antes de la marca no se cuentan, y eso es a proposito.
        _dias = max(0, (int(time.time()) - marca) // 86400)
        _desde = ("hoy" if _dias == 0 else
                  "ayer" if _dias == 1 else f"hace {_dias} días")
        _viejas = 0
        try:
            _c3 = get_conn()
            try:
                _viejas = _c3.execute(
                    "SELECT COUNT(*) c FROM predictions WHERE created_ts<?",
                    (marca,)).fetchone()["c"]
            finally:
                _c3.close()
        except Exception:
            _viejas = 0
        out.append(f"_Acierto medido desde {_desde}_ · "
                   f"{_viejas:,} predicciones anteriores quedan fuera "
                   f"(se puntuaron con el cálculo roto de antes de la "
                   f"Ola 18-G)")
    if ev and ev["n"]:
        out.append(f"Precisión final: *{round(ev['acc'] or 0)}%* "
                   f"({ev['n']} evaluadas)")
        if ev["t"] is not None:
            out.append(f"Tiempo medio a confirmación: {round(ev['t'])}s")
        if ev["chg"] is not None:
            out.append(f"Rendimiento medio del token a la fecha de "
                       f"medición: {round(ev['chg']):+d}%")
        _fp = (f"Falsos positivos (alertó y 0 llegó): {fp}/{alerted} "
               f"evaluadas ({pct(fp, alerted)})")
        if alerted_abiertas:
            _fp += f" · {alerted_abiertas} alertadas aún sin evaluar"
        out.append(_fp)
    out.append(f"\nNiveles → 🟢 {tiers.get('alpha',0)} · "
               f"🟡 {tiers.get('watchlist',0)} · 🔴 {tiers.get('ignored',0)}")
    if leaders:
        out.append("\n*Líderes más fiables:*")
        for l in leaders:
            alias = gmap.get(l["leader"], {}).get("alias", l["leader"][:6])
            out.append(f"• {alias}: {round(l['acc'] or 0)}% ({l['n']} pred.)")
    out += _diag
    out.append("\n_Usa estos datos para recalibrar umbrales y pesos con "
               "evidencia, no con intuición._")
    return "\n".join(out)
