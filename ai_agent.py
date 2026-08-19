"""
Agente conversacional: chat libre en Telegram (mensajes sin /comando).

La IA entiende la intención y usa herramientas:
  - Lectura (consultar base, perfilar billetera): se ejecutan directo.
  - Modificación (descartar, rastrear, correr ciclo): la IA la propone
    y el bot pide confirmación con botones antes de ejecutar.

Desde el 18/8/2026 el agente habla PRIMERO con la IA local (LM Studio,
formato OpenAI de tools) y usa la nube de Claude solo como respaldo,
siguiendo el setting "ia_proveedor" igual que ia_puente.
"""

import json
import os
import re

import requests

from db import get_conn

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"

TOOLS = [
    {"name": "consultar_base",
     "description": ("Lee el snapshot de la base de datos: top billeteras "
                     "(alias, scores, PnL, clase), señales recientes con "
                     "resultados 1h/24h y totales del sistema. Úsala para "
                     "cualquier pregunta sobre los datos."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "perfil_billetera",
     "description": ("Investiga a fondo una billetera on-chain (~1 min): "
                     "actividad, PnL, win rate, retención, huellas de bot "
                     "y Wallet Score 0-100."),
     "input_schema": {"type": "object", "properties": {
         "address": {"type": "string", "description": "dirección Solana"}},
         "required": ["address"]}},
    {"name": "descartar_billetera",
     "description": ("Deja de rastrear una billetera: se marca como "
                     "descartada, sale del top y dejan de llegar sus "
                     "señales. Requiere confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "address": {"type": "string"}}, "required": ["address"]}},
    {"name": "rastrear_billetera",
     "description": ("Reactiva el rastreo de una billetera descartada. "
                     "Requiere confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "address": {"type": "string"}}, "required": ["address"]}},
    {"name": "correr_ciclo",
     "description": ("Corre ahora mismo el ciclo completo de descubrimiento "
                     "y análisis (tarda varios minutos). Requiere "
                     "confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "cambiar_umbral_senal",
     "description": ("Cambia el umbral mínimo del score de señal (0-100). "
                     "Señales de compra con score menor no alertan (pero sí "
                     "se registran y miden). 0 = alertar todo. Fijarlo a "
                     "mano APAGA el auto-ajuste; -1 = volver al modo "
                     "automático. Requiere confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "valor": {"type": "number", "description": "umbral 0-100"}},
         "required": ["valor"]}},
]

# Mismas herramientas en el formato OpenAI que habla LM Studio.
TOOLS_OPENAI = [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in TOOLS]

MODIFYING = {"descartar_billetera", "rastrear_billetera", "correr_ciclo",
             "cambiar_umbral_senal"}

SYSTEM = (
    "Eres el asistente del sistema de rastreo de billeteras rentables en "
    "Solana. Hablas con su dueño por Telegram y tienes memoria de los "
    "últimos mensajes de la conversación. Responde en español, breve "
    "y directo, sin markdown pesado. Abrevia direcciones a 8 caracteres al "
    "mencionarlas (pero pasa la dirección COMPLETA a las herramientas). "
    "Usa las herramientas cuando haga falta; para preguntas de datos usa "
    "consultar_base. Para acciones que modifican, invoca la herramienta "
    "directamente: el sistema le pedirá confirmación al usuario, no tú.")

HISTORY_TURNS = 12   # mensajes de memoria (6 intercambios)
MAX_PASOS = 4        # iteraciones máximas del loop de herramientas


def _load_history(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT role, text FROM chat_history ORDER BY id DESC LIMIT ?",
        (HISTORY_TURNS,)).fetchall()
    return [{"role": r["role"], "content": r["text"]}
            for r in reversed(rows)]


def _save_turn(user_text: str, reply: str):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO chat_history (role, text) VALUES ('user', ?)",
            (user_text[:2000],))
        conn.execute(
            "INSERT INTO chat_history (role, text) VALUES ('assistant', ?)",
            ((reply or "(propuse una acción)")[:2000],))
        conn.execute(
            """DELETE FROM chat_history WHERE id NOT IN
               (SELECT id FROM chat_history ORDER BY id DESC LIMIT 40)""")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"· No se pudo guardar historial de chat: {e}")


def _exec_read(name: str, args: dict) -> str:
    """Ejecuta herramientas de solo lectura."""
    try:
        if name == "consultar_base":
            from ai_chat import _snapshot
            return json.dumps(_snapshot(), ensure_ascii=False,
                              default=str)[:7000]
        if name == "perfil_billetera":
            from wallet_profiler import profile_wallet
            from wallet_score import compute_score
            from signal_tracker import wallet_track_record
            addr = (args.get("address") or "").strip()
            p = profile_wallet(addr)
            if not p["tx_sampled"]:
                return "Sin transacciones recuperadas para esa dirección."
            conn = get_conn()
            tr = wallet_track_record(conn, addr)
            conn.close()
            s = compute_score(p, tr)
            comp = {"wallet_score": s,
                    "txs_muestreadas": p["tx_sampled"],
                    "pnl_total_sol": round(p["pnl_total_sol"], 2),
                    "pnl_30d_sol": round(p.get("pnl_30d_sol", 0), 2),
                    "win_rate_pct": p.get("win_rate_pct"),
                    "retencion_mediana_min": p.get("hold_median_min"),
                    "tokens_operados": len(p["tokens"]),
                    "posible_bot": p["possible_bot"],
                    "flips_1min_pct": p.get("flips_1min_pct"),
                    "horas_activas_24": p.get("active_hours_24"),
                    "track_record": tr}
            return json.dumps(comp, ensure_ascii=False, default=str)
    except Exception as e:
        return f"Error ejecutando {name}: {e}"
    return "Herramienta desconocida."


def _sin_think(texto: str) -> str:
    """Quita bloques <think> que algunos modelos locales incluyen."""
    return re.sub(r"<think>.*?</think>", "", texto or "", flags=re.S).strip()


def _chat_local(messages: list[dict]):
    """Loop del agente contra la IA local (formato OpenAI de tools).

    Devuelve (respuesta, accion) si la local respondió, o None si no está
    disponible (para que el llamador pruebe la nube)."""
    try:
        from decision_ia import _url, _modelo
        conn = get_conn()
        try:
            url, modelo = _url(conn), _modelo(conn)
        finally:
            conn.close()
        if not url:
            return None
        # /no_think: apaga el razonamiento de Qwen — sin el, el modelo se
        # gasta el max_tokens pensando y el chat "no contesta" (18/8).
        msgs = [{"role": "system", "content": SYSTEM + "\n/no_think"}] \
            + messages
        sin_tope = False   # se enciende si el razonamiento se come el tope
        for _ in range(MAX_PASOS):
            cuerpo = {"model": modelo, "temperature": 0.3,
                      "messages": msgs,
                      "chat_template_kwargs": {"enable_thinking": False},
                      "tools": TOOLS_OPENAI}
            if not sin_tope:
                # +200 de colchon por si el modelo ignora /no_think
                # y razona igual (mismo criterio que ia_puente).
                cuerpo["max_tokens"] = 900
            r = requests.post(
                f"{url}/v1/chat/completions", json=cuerpo,
                timeout=150 if sin_tope else 90)
            if r.status_code >= 400:
                print(f"· Agente local HTTP {r.status_code}: {r.text[:200]}")
                return None
            eleccion = r.json()["choices"][0]
            m = eleccion["message"]
            text = _sin_think(m.get("content"))
            tcs = m.get("tool_calls") or []
            if not tcs:
                if not text:
                    fin = eleccion.get("finish_reason")
                    if fin == "length" and not sin_tope:
                        # Mismo remedio que el puente (19/8): el modelo
                        # pensante ignora los apagadores — reintento sin
                        # tope de tokens; el limite real es el timeout.
                        print("· Agente local: razonamiento comio el "
                              "tope; reintento SIN tope de tokens")
                        sin_tope = True
                        continue
                    # Vacio = fallo del modelo, no "no entendi":
                    # devolver None deja caer a la nube.
                    print(f"· Agente local respondio VACIO (finish={fin})")
                    return None
                return text, None
            tc = tcs[0]
            name = tc.get("function", {}).get("name", "")
            raw = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except json.JSONDecodeError:
                args = {}
            if name in MODIFYING:
                return text, {"tool": name, "args": args}
            resultado = _exec_read(name, args)
            # Reinyectar el turno LIMPIO: sin el bloque <think>, que si
            # existiera se acumularia paso a paso (hallazgo 18/8).
            msgs.append({"role": "assistant", "content": text,
                         "tool_calls": tcs})
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": resultado})
        return "Necesité demasiados pasos; intenta ser más específico.", None
    except Exception as e:
        print(f"· Agente local no disponible: {e}")
        return None


def _chat_nube(messages: list[dict]):
    """Loop del agente contra la API de Claude (modo viejo)."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        msgs = list(messages)
        for _ in range(MAX_PASOS):
            r = requests.post(
                API_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 700, "system": SYSTEM,
                      "tools": TOOLS, "messages": msgs},
                timeout=90)
            if r.status_code >= 400:
                print(f"· Agente nube HTTP {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            content = data.get("content", [])
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text").strip()
            tool_calls = [b for b in content if b.get("type") == "tool_use"]
            if not tool_calls:
                return (text or "No entendí, ¿puedes reformular?"), None
            tc = tool_calls[0]
            if tc["name"] in MODIFYING:
                return text, {"tool": tc["name"], "args": tc.get("input", {})}
            resultado = _exec_read(tc["name"], tc.get("input", {}))
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc["id"],
                 "content": resultado}]})
        return "Necesité demasiados pasos; intenta ser más específico.", None
    except Exception as e:
        print(f"· Agente nube no disponible: {e}")
        return None


def chat(user_text: str):
    """
    Corre el loop del agente. Devuelve (respuesta, accion_pendiente).
    accion_pendiente es None o {"tool": ..., "args": {...}} si la IA quiere
    ejecutar una acción que modifica y hay que confirmar.
    """
    if user_text.lower().strip() in ("olvida", "olvida todo", "reset",
                                     "borra la conversacion",
                                     "borra la conversación"):
        conn = get_conn()
        conn.execute("DELETE FROM chat_history")
        conn.commit()
        conn.close()
        return "🧹 Memoria de conversación borrada. Empezamos de cero.", None
    conn = get_conn()
    history = _load_history(conn)
    from ia_puente import _setting
    orden = str(_setting("ia_proveedor", "local_primero", conn)
                or "local_primero")
    conn.close()
    messages = history + [{"role": "user", "content": user_text}]

    if orden == "nube":
        cadena = (_chat_nube, _chat_local)
    elif orden == "local":
        cadena = (_chat_local,)
    else:                                   # local_primero
        cadena = (_chat_local, _chat_nube)
    for intento in cadena:
        res = intento(messages)
        if res is not None:
            respuesta, accion = res
            if accion is None:
                _save_turn(user_text, respuesta)
            else:
                _save_turn(user_text,
                           respuesta or f"Propuse ejecutar {accion['tool']}")
            return respuesta, accion
    return ("Ninguna IA respondió: revisa que LM Studio esté corriendo "
            "(/ialocal <url>) o configura ANTHROPIC_API_KEY."), None


def describe_action(action: dict) -> str:
    tool, args = action["tool"], action.get("args", {})
    addr = (args.get("address") or "")[:12]
    if tool == "descartar_billetera":
        return f"❌ Descartar la billetera `{addr}…` (deja de rastrearse)"
    if tool == "rastrear_billetera":
        return f"⭐ Volver a rastrear la billetera `{addr}…`"
    if tool == "correr_ciclo":
        return "🔄 Correr el ciclo completo de descubrimiento y análisis"
    if tool == "cambiar_umbral_senal":
        return f"🎯 Fijar el umbral mínimo de señal en {args.get('valor')}/100"
    return tool


def execute_action(action: dict) -> str:
    """Ejecuta una acción de modificación ya confirmada por el usuario."""
    tool, args = action["tool"], action.get("args", {})
    try:
        if tool == "descartar_billetera":
            from wallet_admin import discard_wallet
            return discard_wallet((args.get("address") or "").strip())
        if tool == "rastrear_billetera":
            from wallet_admin import restore_wallet
            return restore_wallet((args.get("address") or "").strip())
        if tool == "correr_ciclo":
            from telegram_bot import run_full_cycle
            return run_full_cycle()
        if tool == "cambiar_umbral_senal":
            from db import set_setting
            v_raw = float(args.get("valor", 0))
            if v_raw < 0:
                conn = get_conn()
                set_setting(conn, "umbral_manual", "0")
                conn.close()
                return ("🎚️ Auto-ajuste del umbral reactivado: volverá a "
                        "optimizarse solo con el historial medido.")
            v = max(0, min(100, v_raw))
            conn = get_conn()
            set_setting(conn, "min_signal_score", v)
            # El ajuste MANUAL manda (19/8): sin esta marca, el auto-
            # ajustador de signal_tracker lo pisaba a los 15 minutos.
            set_setting(conn, "umbral_manual", "1")
            conn.close()
            return (f"🎯 Umbral fijado en {v:.0f}/100 (modo manual: el "
                    f"auto-ajuste queda apagado; di «umbral automático» "
                    f"para reactivarlo). Señales de compra con score "
                    f"menor quedarán silenciadas.")
    except Exception as e:
        return f"Error ejecutando la acción: {e}"
    return "Acción desconocida."
