"""
IA local (LM Studio en el PC del dueño) como gestora de salidas del paper.

Fase 2 del copy trading: el paper reparte sus posiciones en dos mitades
(A/B). La mitad "reglas" se gestiona como siempre; en la mitad "ia", cuando
la ⭐ vende TODO, se le pregunta a la IA local que hacer. Con barandillas:

  - Solo puede elegir del menu permitido: {"salida":"vender"} o
    {"salida":"holdear","max_min":5..120}. Cualquier otra cosa se descarta.
  - El stop-loss y el take-profit normales SIGUEN activos siempre: la IA
    no puede quitarlos ni aflojarlos, solo decidir la salida por venta.
  - Si el PC esta apagado, el tunel caido o la respuesta es basura, la
    decision cae a las reglas de siempre (y queda registrado el porque).

La URL del tunel se guarda en settings ("local_ai_url", editable con
/ialocal sin tocar codigo) porque los tuneles rapidos de Cloudflare
cambian de URL al reiniciar.
"""

import json
import os

# Tiempos (v2, auditoria 19/8). La salida tenia TIMEOUT=8 y un POST crudo
# que se saltaba el puente: contra el Qwen pensante CADA llamada volvia
# vacia y el A/B midio reglas-contra-reglas una semana (decidido_por fue
# 3/3 respuesta_vacia en vivo). Ahora las dos decisiones van por el
# puente con paciencia=True: con modelo_pensante ya aprendido el primer
# intento sale sin tope y tarda 5-30 s; el TP/SL duro sigue supremo
# durante la espera, y la llamada corre en el hilo del lote, no frena a
# los demas webhooks.
TIMEOUT_SALIDA = 45    # salida: un intento generoso; a las reglas si no
TIMEOUT_ENTRADA = 60   # filtro de entrada: hilo aparte y modo sombra —
                       # puede esperar a un modelo lento sin frenar nada
MAX_HOLD_MIN = 120    # tope duro al hold que la IA puede pedir
MIN_HOLD_MIN = 5


def _url(conn) -> str | None:
    u = os.getenv("LOCAL_AI_URL", "").strip()
    if not u:
        try:
            from db import get_setting
            u = (get_setting(conn, "local_ai_url", "") or "").strip()
        except Exception:
            u = ""
    return u.rstrip("/") or None


def _modelo(conn) -> str:
    m = os.getenv("LOCAL_AI_MODEL", "").strip()
    if not m:
        try:
            from db import get_setting
            m = (get_setting(conn, "local_ai_model", "") or "").strip()
        except Exception:
            m = ""
    # El Qwen GRANDE, montado permanente para la semana de prueba del
    # experimento (si el A/B no muestra mejora, se retira). Identificador
    # real leido del /v1/models del dueño. OJO: exige desactivar el
    # desmontaje por inactividad en LM Studio; si vuelve el TTL, cambiar
    # al chico ("qwen/qwen3-4b-2507") con el setting "local_ai_model".
    return m or "qwen3.6-35b-a3b"


def armar_contexto(conn, row, price: float, perfil,
                   token: dict | None = None) -> dict:
    """Contexto enriquecido para la decision (pedido del dueño, 17/8):
    ademas del estado de la posicion y el perfil de salida, se inyectan
    liquidez/MC del token, el track record amplio de la billetera, el
    momentum del token desde la señal y cuantas ⭐ siguen DENTRO. Todo
    sale de la base propia con consultas de milisegundos; cada bloque es
    opcional (si falla o no hay dato, simplemente no viaja)."""
    import time as _t
    ctx = {
        "pnl_actual_pct": round((price / row["entry_price"] - 1) * 100, 1),
        "minutos_abierta": round((_t.time() - row["entry_ts"]) / 60),
        "perfil_salida_billetera": perfil or "sin datos aun",
        "precio_entrada": row["entry_price"],
        "precio_ahora": price,
    }
    mint, wallet = row["mint"], row["wallet"]
    # ── Token: liquidez, market cap y momentum desde la señal ──
    try:
        sig = conn.execute(
            """SELECT liq, mc FROM signals
               WHERE mint=? AND (liq IS NOT NULL OR mc IS NOT NULL)
               ORDER BY ts DESC LIMIT 1""", (mint,)).fetchone()
        liq = sig["liq"] if sig else None
        mc_senal = sig["mc"] if sig else None
        mc_ahora = (token or {}).get("mc")
        if liq is not None:
            ctx["liquidez_usd"] = round(liq)
            ctx["nota_liquidez"] = ("BAJA: salir cuesta caro (slippage)"
                                    if liq < 20000 else "suficiente")
        if mc_ahora:
            ctx["market_cap_usd"] = round(mc_ahora)
        if mc_senal and mc_ahora and mc_senal > 0:
            ctx["momentum_mc_desde_senal_pct"] = round(
                (mc_ahora / mc_senal - 1) * 100, 1)
    except Exception:
        pass
    # ── Billetera: track record amplio (aciertos medidos) ──
    try:
        from signal_tracker import wallet_track_record
        tr = wallet_track_record(conn, wallet)
        if tr:
            ctx["track_record_billetera"] = tr
    except Exception:
        pass
    # ── Consenso: ¿cuantas ⭐ siguen DENTRO del token? ──
    try:
        dentro = conn.execute(
            """SELECT COUNT(*) c FROM positions p
               JOIN wallets w ON w.address = p.wallet
                    AND w.is_tracked = 1
               WHERE p.mint=? AND COALESCE(p.tokens, 0) > 0
                 AND p.wallet <> ?""", (mint, wallet)).fetchone()["c"]
        vendieron = conn.execute(
            """SELECT COUNT(DISTINCT s.wallet) c FROM signals s
               JOIN wallets w ON w.address = s.wallet AND w.is_tracked = 1
               WHERE s.mint=? AND s.side='venta'
                 AND s.ts >= ? AND s.wallet <> ?""",
            (mint, int(_t.time()) - 3600, wallet)).fetchone()["c"]
        ctx["otras_estrellas_aun_dentro"] = dentro
        ctx["estrellas_que_vendieron_ultima_hora"] = vendieron
    except Exception:
        pass
    return ctx


def decidir_entrada(conn, trade: dict, token: dict | None) -> dict | None:
    """Veredicto de entrada (modo SOMBRA): ¿valia la pena copiar esta
    compra? Se pregunta DESPUES de abrir la posicion — el camino caliente
    no espera a nadie — y el veredicto se guarda para medir, al cierre,
    cuanto habria ahorrado rechazar. None si la IA no esta o falla.

    Devuelve {"entrada": "copiar"|"rechazar", "razon": "..."} validado,
    o {"entrada": "invalida:<motivo>"} si la IA hablo pero mal (se
    persiste: una respuesta basura ya no es indistinguible de IA
    apagada), o None si la IA no esta / no contesto."""
    # La puerta es hay_ia(), no la URL local (auditoria 19/8): el puente
    # tiene fallback a nube y esta puerta lo mataba cuando el tunel de
    # Cloudflare rotaba de URL.
    from ia_puente import hay_ia
    if not hay_ia():
        return None
    ctx = {"compra_de_la_billetera_sol": trade.get("sol"),
           "mint": (trade.get("mint") or "")[:12]}
    try:
        from signal_tracker import wallet_track_record
        tr = wallet_track_record(conn, trade["wallet"])
        if tr:
            ctx["track_record_billetera"] = tr
    except Exception:
        pass
    try:
        from salidas import perfil_salida
        pf = perfil_salida(conn, trade["wallet"])
        if pf:
            ctx["perfil_salida_billetera"] = pf
    except Exception:
        pass
    try:
        if token and token.get("mc"):
            ctx["market_cap_usd"] = round(token["mc"])
        sig = conn.execute(
            """SELECT liq FROM signals WHERE mint=? AND liq IS NOT NULL
               ORDER BY ts DESC LIMIT 1""", (trade["mint"],)).fetchone()
        if sig and sig["liq"] is not None:
            ctx["liquidez_usd"] = round(sig["liq"])
            if sig["liq"] < 20000:
                ctx["nota_liquidez"] = "BAJA: entrar y salir cuesta caro"
        dentro = conn.execute(
            """SELECT COUNT(*) c FROM positions p
               JOIN wallets w ON w.address=p.wallet AND w.is_tracked=1
               WHERE p.mint=? AND COALESCE(p.tokens,0) > 0""",
            (trade["mint"],)).fetchone()["c"]
        ctx["estrellas_dentro_del_token"] = dentro
    except Exception:
        pass

    prompt = (
        "Eres el filtro de entradas de un copy trading de memecoins en "
        "Solana. Una billetera rastreada acaba de COMPRAR y nuestro "
        "sistema la copio. Con su historial y el contexto, ¿valia la pena "
        "copiar esta compra o era una mala señal? Datos:\n"
        + json.dumps(ctx, ensure_ascii=False, default=str)
        + "\n\nResponde SOLO este JSON, sin nada mas:\n"
        '{"entrada":"copiar"} o {"entrada":"rechazar","razon":"una frase"}')
    try:
        # Via el puente con menu cerrado (v2, 19/8): hereda /no_think, el
        # apagado por plantilla, la limpieza de <think> y el reintento
        # anti-razonamiento (paciencia=True: hilo de fondo, modo sombra).
        from ia_puente import completar_json
        v, _prov, motivo = completar_json(
            prompt, {"entrada": ("copiar", "rechazar")},
            max_tokens=300, timeout=TIMEOUT_ENTRADA, conn=conn,
            paciencia=True)
        if v is not None:
            return {"entrada": v["entrada"],
                    "razon": str(v.get("razon", ""))[:120]}
        if motivo != "sin_respuesta":
            # Hablo pero mal: se registra el porque (antes se perdia y la
            # columna ia_entrada quedaba NULL — en vivo, 'copiar' no se
            # registro jamas por esto).
            return {"entrada": f"invalida:{motivo}"[:40], "razon": ""}
    except Exception as e:
        print(f"· IA entrada falló: {e}")
    return None


def decidir_salida(conn, contexto: dict) -> dict:
    """Pregunta a la IA local que hacer cuando la ⭐ vendio todo.

    Devuelve SIEMPRE un dict valido:
      {"salida": "vender"}                     → cerrar ya
      {"salida": "holdear", "max_min": N}      → hold con trailing (N acotado)
    y ademas "decidido_por": "ia_local" | "reglas_fallback:<motivo>".

    v2 (auditoria 19/8): via el puente. La version anterior hacia su
    propio POST con max_tokens=320 y timeout=8 y NUNCA recibio el arreglo
    del modelo pensante: contra el Qwen 35B cada llamada volvia vacia y
    el A/B llevaba una semana midiendo reglas-contra-reglas (en vivo:
    decidido_por 3/3 'respuesta_vacia'). Un solo camino a la IA.
    """
    from ia_puente import hay_ia, completar_json
    if not hay_ia():
        return {"salida": "vender",
                "decidido_por": "reglas_fallback:sin_ia"}
    prompt = (
        "Eres el gestor de salidas de un copy trading de memecoins en "
        "Solana. La billetera que copiamos acaba de VENDER TODO su token. "
        "Decide si copiamos su venta ya o si mantenemos la posicion un "
        "rato mas con trailing stop (el stop-loss duro sigue activo "
        "pase lo que pase). Datos:\n"
        + json.dumps(contexto, ensure_ascii=False, default=str)
        + "\n\nResponde SOLO este JSON, sin nada mas:\n"
        '{"salida":"vender"} o {"salida":"holdear","max_min":<5-120>,'
        '"razon":"una frase"}')
    v, _prov, motivo = completar_json(
        prompt, {"salida": ("vender", "holdear")},
        max_tokens=320, timeout=TIMEOUT_SALIDA, conn=conn, paciencia=True)
    if v is None:
        det = ("respuesta_vacia" if motivo == "sin_respuesta"
               else f"invalida_{motivo}")
        return {"salida": "vender",
                "decidido_por": f"reglas_fallback:{det}"[:60]}
    if v["salida"] == "vender":
        return {"salida": "vender", "decidido_por": "ia_local",
                "razon": str(v.get("razon", ""))[:120]}
    # holdear: max_min es OBLIGATORIO y numerico. Antes un holdear sin
    # max_min se convertia en hold de 5 min etiquetado ia_local; ahora
    # una respuesta incompleta cae a reglas con su motivo.
    try:
        mins = float(v.get("max_min"))
    except (TypeError, ValueError):
        return {"salida": "vender",
                "decidido_por": "reglas_fallback:invalida_max_min"}
    mins = max(MIN_HOLD_MIN, min(MAX_HOLD_MIN, mins))
    return {"salida": "holdear", "max_min": mins,
            "decidido_por": "ia_local",
            "razon": str(v.get("razon", ""))[:120]}
