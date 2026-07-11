"""
Agente conversacional: chat libre en Telegram (mensajes sin /comando).

La IA entiende la intención y usa herramientas:
  - Lectura (consultar base, perfilar billetera): se ejecutan directo.
  - Modificación (descartar, rastrear, correr ciclo): la IA la propone
    y el bot pide confirmación con botones antes de ejecutar.
"""

import json
import os

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
]

MODIFYING = {"descartar_billetera", "rastrear_billetera", "correr_ciclo"}

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


def chat(user_text: str):
    """
    Corre el loop del agente. Devuelve (respuesta, accion_pendiente).
    accion_pendiente es None o {"tool": ..., "args": {...}} si la IA quiere
    ejecutar una acción que modifica y hay que confirmar.
    """
    if not ANTHROPIC_API_KEY:
        return "Falta ANTHROPIC_API_KEY para el chat.", None
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
    conn.close()
    messages = history + [{"role": "user", "content": user_text}]
    try:
        for _ in range(4):
            r = requests.post(
                API_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 700, "system": SYSTEM,
                      "tools": TOOLS, "messages": messages},
                timeout=90)
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [])
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text").strip()
            tool_calls = [b for b in content if b.get("type") == "tool_use"]
            if not tool_calls:
                respuesta = text or "No entendí, ¿puedes reformular?"
                _save_turn(user_text, respuesta)
                return respuesta, None

            tc = tool_calls[0]
            if tc["name"] in MODIFYING:
                _save_turn(user_text,
                           text or f"Propuse ejecutar {tc['name']}")
                return text, {"tool": tc["name"],
                              "args": tc.get("input", {})}
            resultado = _exec_read(tc["name"], tc.get("input", {}))
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc["id"],
                 "content": resultado}]})
        respuesta = "Necesité demasiados pasos; intenta ser más específico."
        _save_turn(user_text, respuesta)
        return respuesta, None
    except Exception as e:
        return f"Error en el chat: {e}", None


def describe_action(action: dict) -> str:
    tool, args = action["tool"], action.get("args", {})
    addr = (args.get("address") or "")[:12]
    if tool == "descartar_billetera":
        return f"❌ Descartar la billetera `{addr}…` (deja de rastrearse)"
    if tool == "rastrear_billetera":
        return f"⭐ Volver a rastrear la billetera `{addr}…`"
    if tool == "correr_ciclo":
        return "🔄 Correr el ciclo completo de descubrimiento y análisis"
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
    except Exception as e:
        return f"Error ejecutando la acción: {e}"
    return "Acción desconocida."
