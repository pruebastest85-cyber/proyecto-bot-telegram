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

import requests

# 8 s: el Qwen 35B-A3B queda MONTADO de forma permanente durante la
# semana de prueba (decision del dueño, 17/8/2026: el proyecto de
# imagenes esta en pausa, la GPU es del bot). Sin recargas just-in-time,
# la inferencia son 2-4 s; 8 s cubre tunel y GPU ocupada. Esta llamada
# corre en el hilo del webhook: es un maximo, no un objetivo.
TIMEOUT = 8
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

    Devuelve {"entrada": "copiar"|"rechazar", "razon": "..."} validado."""
    url = _url(conn)
    if not url:
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
        r = requests.post(
            f"{url}/v1/chat/completions",
            json={"model": _modelo(conn), "temperature": 0.2,
                  "max_tokens": 100,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=TIMEOUT)
        if r.status_code >= 400:
            print(f"· IA entrada HTTP {r.status_code}: {r.text[:200]}")
            return None
        texto = (r.json()["choices"][0]["message"]["content"] or "")
        t = texto.replace("```json", "").replace("```", "").strip()
        try:
            v = json.loads(t)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", t, flags=re.S)
            if not m:
                raise
            v = json.loads(m.group(0))
        e = str(v.get("entrada", "")).lower().strip()
        if e in ("copiar", "rechazar"):
            return {"entrada": e, "razon": str(v.get("razon", ""))[:120]}
    except Exception as e:
        print(f"· IA entrada falló: {e}")
    return None


def decidir_salida(conn, contexto: dict) -> dict:
    """Pregunta a la IA local que hacer cuando la ⭐ vendio todo.

    Devuelve SIEMPRE un dict valido:
      {"salida": "vender"}                     → cerrar ya
      {"salida": "holdear", "max_min": N}      → hold con trailing (N acotado)
    y ademas "decidido_por": "ia_local" | "reglas_fallback:<motivo>".
    """
    url = _url(conn)
    if not url:
        return {"salida": "vender",
                "decidido_por": "reglas_fallback:sin_url"}
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
    try:
        r = requests.post(
            f"{url}/v1/chat/completions",
            json={"model": _modelo(conn), "temperature": 0.2,
                  "max_tokens": 120,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=TIMEOUT)
        if r.status_code >= 400:
            print(f"· IA local HTTP {r.status_code}: {r.text[:200]}")
            return {"salida": "vender",
                    "decidido_por": f"reglas_fallback:http_{r.status_code}"}
        texto = (r.json()["choices"][0]["message"]["content"] or "")
    except Exception as e:
        print(f"· IA local inalcanzable: {e}")
        return {"salida": "vender",
                "decidido_por": "reglas_fallback:inalcanzable"}

    # Parseo tolerante (igual que con las IAs de nube: no tirar una
    # respuesta por venir envuelta en texto) + validacion ESTRICTA del
    # contenido: fuera del menu permitido no se obedece nada.
    try:
        t = texto.replace("```json", "").replace("```", "").strip()
        try:
            v = json.loads(t)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", t, flags=re.S)
            if not m:
                raise
            v = json.loads(m.group(0))
        salida = str(v.get("salida", "")).lower().strip()
        if salida == "vender":
            return {"salida": "vender", "decidido_por": "ia_local",
                    "razon": str(v.get("razon", ""))[:120]}
        if salida == "holdear":
            mins = float(v.get("max_min", 0))
            mins = max(MIN_HOLD_MIN, min(MAX_HOLD_MIN, mins))
            return {"salida": "holdear", "max_min": mins,
                    "decidido_por": "ia_local",
                    "razon": str(v.get("razon", ""))[:120]}
    except Exception as e:
        print(f"· IA local respondio basura ({e}): {texto[:120]}")
    return {"salida": "vender",
            "decidido_por": "reglas_fallback:respuesta_invalida"}
