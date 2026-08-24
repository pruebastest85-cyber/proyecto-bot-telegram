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
import threading

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
    # ── Consultas con parámetros (Ola 9, 21/8): la IA ya no depende del
    # snapshot fijo — puede pedir datos concretos de la base y encadenar. ──
    {"name": "tokens_con_estrellas",
     "description": ("Tokens donde MÁS billeteras ⭐ distintas compraron "
                     "en las últimas N horas, con cuántas entraron y "
                     "cuándo. Ideal para '¿dónde se está metiendo la smart "
                     "money ahora?'"),
     "input_schema": {"type": "object", "properties": {
         "horas": {"type": "number",
                   "description": "ventana en horas, 1 a 48 (default 6)"}}}},
    {"name": "buscar_billetera",
     "description": ("Busca billeteras por alias o trozo de dirección. "
                     "Devuelve dirección completa, alias, grado, score, "
                     "clase IA y si está rastreada. Úsala SIEMPRE que el "
                     "usuario nombre una billetera por su alias."),
     "input_schema": {"type": "object", "properties": {
         "texto": {"type": "string", "description": "alias o fragmento"}},
         "required": ["texto"]}},
    {"name": "senales_de_billetera",
     "description": ("Últimas señales (compras/ventas) de UNA billetera con "
                     "su resultado medido a 1h/24h."),
     "input_schema": {"type": "object", "properties": {
         "address": {"type": "string", "description": "dirección completa"},
         "n": {"type": "number", "description": "cuántas, máx 20"}},
         "required": ["address"]}},
    {"name": "top_billeteras",
     "description": ("El top N del ranking real (el que alerta y se copia) "
                     "con alias, score, PnL y clase."),
     "input_schema": {"type": "object", "properties": {
         "n": {"type": "number", "description": "cuántas, máx 30"}}}},
    {"name": "evidencia_billetera",
     "description": ("El PORQUÉ de una billetera: sus apariciones tempranas "
                     "en tokens ganadores registradas por el sistema."),
     "input_schema": {"type": "object", "properties": {
         "address": {"type": "string"}}, "required": ["address"]}},
    {"name": "estado_sistema",
     "description": ("Estado en vivo por sección: 'paper' (simulación y "
                     "PnL), 'posiciones' (paper abiertas), 'rendimiento' "
                     "(win rate de señales), 'salud' (chequeos del "
                     "sistema), 'helius' (créditos del ciclo)."),
     "input_schema": {"type": "object", "properties": {
         "seccion": {"type": "string",
                     "description": ("una de: paper, posiciones, "
                                     "rendimiento, salud, helius")}},
         "required": ["seccion"]}},
    {"name": "ver_ajustes",
     "description": ("Los AJUSTES ACTUALES del bot leídos de la base en "
                     "este momento: umbral de señal, top de alertas, "
                     "paper (encendido, tope, TP/SL) y los últimos "
                     "cambios hechos por chat. ÚSALA SIEMPRE antes de "
                     "afirmar el valor de un ajuste o de proponer "
                     "cambiarlo — nunca cites un ajuste de memoria."),
     "input_schema": {"type": "object", "properties": {}}},
    # ── Ajustes con confirmación (Ola 9): lista blanca y rangos duros. ──
    {"name": "cambiar_top_alertas",
     "description": ("Cambia cuántas billeteras del top alertan y disparan "
                     "la copia (0 = sin límite, todas las ⭐). Requiere "
                     "confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "n": {"type": "number", "description": "0 a 100"}},
         "required": ["n"]}},
    {"name": "configurar_paper",
     "description": ("Configura el paper trading: encenderlo/apagarlo o "
                     "cambiar el tope de SOL por señal. Requiere "
                     "confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "accion": {"type": "string",
                    "description": "una de: encender, apagar, max_sol"},
         "valor": {"type": "number",
                   "description": ("solo para max_sol: tope en SOL, "
                                   "0.05 a 10")}},
         "required": ["accion"]}},
    {"name": "deshacer_ajuste",
     "description": ("Revierte el ÚLTIMO ajuste hecho por chat (umbral, "
                     "top alertas o paper) a su valor anterior. Requiere "
                     "confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "cambiar_umbral_senal",
     "description": ("Cambia el umbral mínimo del score de señal (0-100). "
                     "Señales de compra con score menor no alertan (pero sí "
                     "se registran y miden). 0 = alertar todo. Fijarlo a "
                     "mano APAGA el auto-ajuste; -1 = volver al modo "
                     "automático. Requiere confirmación del usuario."),
     "input_schema": {"type": "object", "properties": {
         "valor": {"type": "number",
                   "description": ("umbral 0-100, o -1 para volver al "
                                   "modo automático (0 significa alertar "
                                   "TODO en modo manual, no es el "
                                   "automático)")}},
         "required": ["valor"]}},
]

# Mismas herramientas en el formato OpenAI que habla LM Studio.
TOOLS_OPENAI = [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in TOOLS]

MODIFYING = {"descartar_billetera", "rastrear_billetera", "correr_ciclo",
             "cambiar_umbral_senal", "cambiar_top_alertas",
             "configurar_paper", "deshacer_ajuste"}

SYSTEM = (
    "Eres el asistente del sistema de rastreo de billeteras rentables en "
    "Solana. Hablas con su dueño por Telegram y tienes memoria de los "
    "últimos mensajes de la conversación. Responde en español, breve "
    "y directo, sin markdown pesado. Abrevia direcciones a 8 caracteres al "
    "mencionarlas (pero pasa la dirección COMPLETA a las herramientas). "
    "Usa las herramientas cuando haga falta y encadénalas si la pregunta "
    "lo pide (p. ej. buscar_billetera para resolver un alias y luego "
    "senales_de_billetera con la dirección). Cita SOLO números que salgan "
    "de los resultados de las herramientas: si no consultaste, no inventes. "
    "Antes de afirmar el valor de un ajuste (umbral, top de alertas, "
    "paper) o de proponer cambiarlo, consulta ver_ajustes: puede que ya "
    "esté en el valor pedido y entonces basta con decirlo, sin proponer "
    "nada. "
    "Para acciones que modifican, invoca la herramienta directamente: el "
    "sistema le pedirá confirmación al usuario, no tú. El bot NO puede "
    "mover dinero real; el paper trading es simulación.")

# (Ola 16) Candado del agente. Tres cosas lo necesitan desde que los
# updates se procesan en paralelo:
#   · LM Studio SERIALIZA las peticiones: dos chats a la vez hacen que el
#     segundo agote su timeout y caiga a la nube (de pago) sin necesidad.
#   · chat_history se lee al principio y se escribe al final: dos turnos
#     solapados se pisan y el agente parece desmemoriado.
#   · ajustes_log es leer-modificar-escribir: dos confirmaciones a la vez
#     perdían una entrada y "deshacer" revertía el ajuste equivocado.
_AGENTE_LOCK = threading.Lock()

HISTORY_TURNS = 12   # mensajes de memoria (6 intercambios)
MAX_PASOS = 4        # iteraciones máximas del loop de herramientas


def _load_history(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT role, text FROM chat_history ORDER BY id DESC LIMIT ?",
        (HISTORY_TURNS,)).fetchall()
    return [{"role": r["role"], "content": r["text"]}
            for r in reversed(rows)]


def _save_turn(user_text: str, reply: str):
    conn = None
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
    except Exception as e:
        print(f"· No se pudo guardar historial de chat: {e}")
    finally:
        if conn is not None:          # (Ola 15 - M7) cerrar siempre
            try:
                conn.close()
            except Exception:
                pass


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
            conn = get_conn()          # (Ola 15 - M7) sin fugas
            try:
                tr = wallet_track_record(conn, addr)
            finally:
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
        if name == "tokens_con_estrellas":
            import time as _t
            try:
                horas = max(1.0, min(48.0, float(args.get("horas") or 6)))
            except (TypeError, ValueError):
                horas = 6.0
            conn = get_conn()
            try:
                rows = conn.execute(
                    """SELECT s.mint, MAX(s.symbol) symbol,
                              COUNT(DISTINCT s.wallet) estrellas,
                              MIN(s.ts) primera_ts, MAX(s.ts) ultima_ts
                       FROM signals s JOIN wallets w
                         ON w.address = s.wallet AND w.is_tracked = 1
                       WHERE s.side='compra' AND s.ts >= ?
                       GROUP BY s.mint
                       ORDER BY estrellas DESC, ultima_ts DESC
                       LIMIT 12""",
                    (int(_t.time() - horas * 3600),)).fetchall()
                ahora = _t.time()
                out = [{"mint": r["mint"], "symbol": r["symbol"],
                        "estrellas_compraron": r["estrellas"],
                        "primera_hace_min": round((ahora - r["primera_ts"]) / 60),
                        "ultima_hace_min": round((ahora - r["ultima_ts"]) / 60)}
                       for r in rows]
            finally:
                conn.close()
            return json.dumps({"ventana_horas": horas, "tokens": out},
                              ensure_ascii=False)[:4000]
        if name == "buscar_billetera":
            texto = (args.get("texto") or "").strip()
            if len(texto) < 2:
                return "Dame al menos 2 caracteres del alias o dirección."
            conn = get_conn()
            try:
                rows = conn.execute(
                    """SELECT address, alias, grade, wallet_score, ai_class,
                              is_tracked, winning_tokens_count
                       FROM wallets
                       WHERE alias LIKE ? OR address LIKE ?
                       ORDER BY COALESCE(is_tracked,0) DESC,
                                COALESCE(wallet_score,0) DESC
                       LIMIT 8""",
                    (f"%{texto}%", f"{texto}%")).fetchall()
            finally:
                conn.close()
            if not rows:
                return f"Ninguna billetera coincide con {texto!r}."
            return json.dumps([dict(r) for r in rows],
                              ensure_ascii=False, default=str)[:4000]
        if name == "senales_de_billetera":
            import time as _t
            addr = (args.get("address") or "").strip()
            try:
                n = max(1, min(20, int(float(args.get("n") or 10))))
            except (TypeError, ValueError):
                n = 10
            conn = get_conn()
            try:
                rows = conn.execute(
                    """SELECT symbol, mint, side, sol, ts, chg_1h, chg_24h
                       FROM signals WHERE wallet=?
                       ORDER BY ts DESC LIMIT ?""", (addr, n)).fetchall()
            finally:
                conn.close()
            if not rows:
                return "Esa billetera no tiene señales registradas."
            ahora = _t.time()
            out = [{"symbol": r["symbol"] or r["mint"][:8],
                    "side": r["side"], "sol": round(r["sol"] or 0, 2),
                    "hace_h": round((ahora - r["ts"]) / 3600, 1),
                    "chg_1h_pct": r["chg_1h"], "chg_24h_pct": r["chg_24h"]}
                   for r in rows]
            return json.dumps(out, ensure_ascii=False)[:4000]
        if name == "top_billeteras":
            try:
                n = max(3, min(30, int(float(args.get("n") or 10))))
            except (TypeError, ValueError):
                n = 10
            from db import top_wallets
            conn = get_conn()
            try:
                rows = top_wallets(conn, n)
                out = [{"puesto": i, "alias": r["alias"],
                        "address": r["address"],
                        "score": r["wallet_score"],
                        "pnl_total_sol_ult_evaluacion": r["pnl_total"]}
                       for i, r in enumerate(rows, 1)]
            finally:
                conn.close()
            return json.dumps(out, ensure_ascii=False, default=str)[:4000]
        if name == "evidencia_billetera":
            from db import wallet_evidence
            addr = (args.get("address") or "").strip()
            conn = get_conn()
            try:
                rows = wallet_evidence(conn, addr)
            finally:
                conn.close()
            if not rows:
                return "Sin evidencia registrada para esa dirección."
            return json.dumps([r["reason"] for r in rows[:10]],
                              ensure_ascii=False)[:4000]
        if name == "ver_ajustes":
            from db import get_setting
            conn = get_conn()
            try:
                from db import TOP_ALERTAS_DEFAULT as _TAD
                umbral = float(get_setting(conn, "min_signal_score",
                                           "0") or 0)
                manual = (get_setting(conn, "umbral_manual", "0")
                          or "0").strip() == "1"
                top = int(float(get_setting(conn, "top_alertas",
                                            _TAD) or _TAD))
                out = {
                    "umbral_senal": {
                        "valor": umbral,
                        "modo": "manual" if manual else "automático",
                        "nota": ("0 en modo manual = alerta TODO"
                                 if manual and umbral == 0 else "")},
                    "top_alertas": {"valor": top,
                                    "nota": "0 = sin límite, todas las ⭐"
                                    if top == 0 else ""},
                    "paper": {
                        "encendido": (get_setting(conn, "paper_enabled",
                                                  "1") or "1") != "0",
                        "max_sol": float(get_setting(
                            conn, "paper_max_sol", "1.0") or 1.0),
                        "tp_pct": float(get_setting(
                            conn, "paper_tp_pct", "100") or 100),
                        "sl_pct": float(get_setting(
                            conn, "paper_sl_pct", "50") or 50)},
                }
                try:
                    log = json.loads(get_setting(conn, "ajustes_log",
                                                 "[]") or "[]")
                    out["ultimos_cambios_por_chat"] = log[-5:]
                except (ValueError, TypeError):
                    pass
            finally:
                conn.close()
            return json.dumps(out, ensure_ascii=False)[:3000]
        if name == "estado_sistema":
            seccion = (args.get("seccion") or "").lower().strip()
            if seccion == "paper":
                from paper_trading import resumen_text
                return resumen_text()[:4000]
            if seccion == "posiciones":
                import time as _t
                conn = get_conn()
                try:
                    rows = conn.execute(
                        """SELECT symbol, mint, entry_price, entry_ts,
                                  stake_sol, origen
                           FROM paper_trades WHERE status='abierta'
                           ORDER BY entry_ts DESC LIMIT 15""").fetchall()
                finally:
                    conn.close()
                if not rows:
                    return "No hay posiciones de paper abiertas."
                ahora = _t.time()
                out = [{"symbol": r["symbol"],
                        "abierta_hace_h": round((ahora - r["entry_ts"]) / 3600, 1),
                        "stake_sol": r["stake_sol"],
                        "origen": r["origen"] or "top"} for r in rows]
                return json.dumps(out, ensure_ascii=False)[:4000]
            if seccion == "rendimiento":
                from rendimiento import rendimiento_text
                return rendimiento_text()[:4000]
            if seccion == "salud":
                from salud import salud_text
                return salud_text()[:4000]
            if seccion == "helius":
                from helius_budget import estado_line
                conn = get_conn()
                try:
                    return estado_line(conn)
                finally:
                    conn.close()
            return ("Sección desconocida. Usa: paper, posiciones, "
                    "rendimiento, salud o helius.")
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
            # (Ola 15 - M4) Responder TODOS los tool_calls del turno:
            # antes solo se ejecutaba tcs[0] pero se reinyectaban todos,
            # y el proveedor rechazaba la peticion siguiente (tool_call
            # sin resultado) → "Ninguna IA respondio" con las dos IA
            # sanas. Si alguno modifica, se propone tras responder las
            # lecturas del mismo turno.
            _pendiente = None
            _resultados = []
            for _tc in tcs:
                _name = _tc.get("function", {}).get("name", "")
                _raw = _tc.get("function", {}).get("arguments") or "{}"
                try:
                    _args = (_raw if isinstance(_raw, dict)
                             else json.loads(_raw))
                except json.JSONDecodeError:
                    _args = {}
                if _name in MODIFYING:
                    if _pendiente is None:
                        _pendiente = {"tool": _name, "args": _args}
                    _resultados.append((_tc.get("id", ""),
                                        "Propuesta al usuario; espera "
                                        "su confirmación."))
                    continue
                _resultados.append((_tc.get("id", ""),
                                    _exec_read(_name, _args)))
            if _pendiente is not None and len(tcs) == 1:
                return text, _pendiente
            # Reinyectar el turno LIMPIO: sin el bloque <think>, que si
            # existiera se acumularia paso a paso (hallazgo 18/8).
            msgs.append({"role": "assistant", "content": text,
                         "tool_calls": tcs})
            for _tid, _res in _resultados:
                msgs.append({"role": "tool", "tool_call_id": _tid,
                             "content": _res})
            if _pendiente is not None:
                return text, _pendiente
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

    (Ola 16) Serializado: ver _AGENTE_LOCK. Si ya hay una consulta en
    curso se avisa en vez de encolar en silencio (la IA local tarda
    minutos y el usuario creería que el bot se colgó)."""
    if not _AGENTE_LOCK.acquire(timeout=1):
        return ("⏳ Estoy contestando tu pregunta anterior (la IA local "
                "va de una en una). Dame un momento y repite."), None
    try:
        return _chat_serializado(user_text)
    finally:
        _AGENTE_LOCK.release()


def _chat_serializado(user_text: str):
    if user_text.lower().strip() in ("olvida", "olvida todo", "reset",
                                     "borra la conversacion",
                                     "borra la conversación"):
        conn = get_conn()          # (Ola 15 - M7) sin fugas
        try:
            conn.execute("DELETE FROM chat_history")
            conn.commit()
        finally:
            conn.close()
        return "🧹 Memoria de conversación borrada. Empezamos de cero.", None
    conn = get_conn()
    try:
        history = _load_history(conn)
        from ia_puente import _setting
        orden = str(_setting("ia_proveedor", "local_primero", conn)
                    or "local_primero")
    finally:
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
    return ("Ninguna IA respondió en este momento. Lo más probable es "
            "que la IA local esté ocupada (un ciclo de evaluación la "
            "acapara varios minutos): reintenta en un rato. Si persiste, "
            "revisa que LM Studio esté corriendo (/ialocal <url>) o "
            "configura ANTHROPIC_API_KEY."), None


def _registrar_ajuste(conn, cambios, antes=None, despues=None):
    """Bitácora de ajustes hechos por chat. Pila de 10 en 'ajustes_log'.

    (Ola 15 - M1) Un ajuste puede tocar VARIAS claves a la vez: fijar el
    umbral escribe min_signal_score Y umbral_manual. Antes solo se
    registraba la primera, y "deshacer" restauraba el valor dejando el
    auto-ajuste apagado para siempre: el estado restaurado no era el
    estado anterior. Ahora `cambios` es {clave: (antes, despues)}; se
    admite la forma vieja (clave, antes, despues) por compatibilidad.
    Un `antes` None significa "el setting NO existía" y deshacer lo
    borra en vez de escribir un default inventado."""
    import time as _t
    from db import get_setting, set_setting
    if isinstance(cambios, str):
        cambios = {cambios: (antes, despues)}
    try:
        log = json.loads(get_setting(conn, "ajustes_log", "[]") or "[]")
    except (ValueError, TypeError):
        log = []
    log.append({"ts": int(_t.time()),
                "claves": {k: {"antes": v[0], "despues": v[1]}
                           for k, v in cambios.items()}})
    set_setting(conn, "ajustes_log", json.dumps(log[-10:],
                                                ensure_ascii=False))


def _deshacer_ultimo(conn) -> str:
    from db import get_setting, set_setting
    try:
        log = json.loads(get_setting(conn, "ajustes_log", "[]") or "[]")
    except (ValueError, TypeError):
        log = []
    if not log:
        return "No hay ajustes hechos por chat que deshacer."
    ult = log.pop()
    # Formato viejo (una clave suelta) y nuevo (varias) conviven.
    claves = ult.get("claves")
    if not claves:
        claves = {ult.get("clave"): {"antes": ult.get("antes"),
                                     "despues": ult.get("despues")}}
    partes = []
    for k, v in claves.items():
        if not k:
            continue
        if v.get("antes") is None:
            try:
                conn.execute("DELETE FROM settings WHERE key=?", (k,))
                conn.commit()
            except Exception:
                set_setting(conn, k, "")
            partes.append(f"{k} vuelve a su valor por defecto")
        else:
            set_setting(conn, k, str(v["antes"]))
            partes.append(f"{k}: {v.get('despues')} → {v['antes']}")
    set_setting(conn, "ajustes_log", json.dumps(log, ensure_ascii=False))
    return "↩️ Deshecho — " + " · ".join(partes) + "."


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
        try:
            if float(args.get("valor", 0)) < 0:
                return "🎯 Volver el umbral de señal al modo AUTOMÁTICO"
        except (TypeError, ValueError):
            pass
        return f"🎯 Fijar el umbral mínimo de señal en {args.get('valor')}/100"
    if tool == "cambiar_top_alertas":
        try:
            n = int(float(args.get("n", 0)))
        except (TypeError, ValueError):
            n = 0
        return ("📡 Quitar el límite: alertarán TODAS las ⭐" if n == 0
                else f"📡 Que alerten solo las top {n} billeteras")
    if tool == "configurar_paper":
        acc = (args.get("accion") or "").lower().strip()
        if acc == "encender":
            return "🧪 ENCENDER el paper trading (simulación)"
        if acc == "apagar":
            return "🧪 APAGAR el paper trading (simulación)"
        if acc == "max_sol":
            return (f"🧪 Fijar el tope del paper en "
                    f"{args.get('valor')} SOL por señal")
        return f"🧪 Paper: acción {acc!r} (no reconocida)"
    if tool == "deshacer_ajuste":
        return "↩️ Deshacer el último ajuste hecho por chat"
    return tool


# (Ola 17-B) Acciones que tardan MINUTOS U HORAS (descubrimiento +
# análisis con Helius). No pueden ir bajo _AGENTE_LOCK: ese candado
# existe para serializar la IA local y proteger la bitácora, y mientras
# lo retenía un ciclo completo, TODO mensaje al chat contestaba
# "⏳ Estoy contestando tu pregunta anterior" — una respuesta falsa (no
# había ninguna pregunta en curso) durante todo el ciclo. Van con su
# propio candado, que sí dice la verdad si ya hay uno corriendo.
_ACCIONES_LARGAS = {"correr_ciclo"}
_LARGA_LOCK = threading.Lock()


def execute_action(action: dict) -> str:
    """Ejecuta una acción de modificación ya confirmada por el usuario."""
    tool = action.get("tool")
    if tool in _ACCIONES_LARGAS:
        if not _LARGA_LOCK.acquire(blocking=False):
            return ("⏳ Ya hay un ciclo completo en marcha (tarda varios "
                    "minutos). Cuando termine te aviso; mientras tanto el "
                    "chat sigue funcionando normal.")
        try:
            return _execute_action_serializado(action)
        finally:
            _LARGA_LOCK.release()
    # (Ola 16) Mismo candado: ajustes_log es leer-modificar-escribir y dos
    # confirmaciones simultáneas perdían una entrada de la bitácora.
    with _AGENTE_LOCK:
        return _execute_action_serializado(action)


def _execute_action_serializado(action: dict) -> str:
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
        if tool == "cambiar_top_alertas":
            from db import get_setting, set_setting
            try:
                n = max(0, min(100, int(float(args.get("n", 0)))))
            except (TypeError, ValueError):
                return "Valor inválido para top de alertas (usa 0-100)."
            conn = get_conn()
            try:
                # (Ola 15 - M2) El default REAL del sistema es
                # TOP_ALERTAS_DEFAULT (15), no 30: con el setting sin
                # fijar, el agente informaba 30 mientras el filtro usaba
                # 15, y un "deshacer" escribía un valor que nunca existió.
                from db import TOP_ALERTAS_DEFAULT as _TAD
                antes = get_setting(conn, "top_alertas", None)
                try:                       # (Ola 16) "" no debe lanzar
                    _efectivo = int(float(
                        antes if antes not in (None, "") else _TAD))
                except (TypeError, ValueError):
                    _efectivo = int(_TAD)
                if _efectivo == n:
                    return (f"El top de alertas YA estaba en {_efectivo}. "
                            f"No cambié nada.")
                set_setting(conn, "top_alertas", str(n))
                _registrar_ajuste(conn, {"top_alertas": (antes, str(n))})
            finally:
                conn.close()
            return ("📡 Sin límite: ahora alertan TODAS las ⭐." if n == 0
                    else f"📡 Hecho: alertan las top {n} billeteras "
                         f"(antes: {antes}).")
        if tool == "configurar_paper":
            from db import get_setting, set_setting
            acc = (args.get("accion") or "").lower().strip()
            conn = get_conn()
            try:
                if acc in ("encender", "apagar"):
                    antes = get_setting(conn, "paper_enabled", None)
                    nuevo = "1" if acc == "encender" else "0"
                    if (antes or "1").strip() == nuevo:
                        return ("El paper YA estaba "
                                + ("encendido." if nuevo == "1"
                                   else "apagado.") + " No cambié nada.")
                    set_setting(conn, "paper_enabled", nuevo)
                    _registrar_ajuste(conn,
                                      {"paper_enabled": (antes, nuevo)})
                    return ("🧪 Paper trading ENCENDIDO." if nuevo == "1"
                            else "🧪 Paper trading APAGADO (las posiciones "
                                 "abiertas se siguen gestionando).")
                if acc == "max_sol":
                    try:
                        v = float(args.get("valor"))
                    except (TypeError, ValueError):
                        return "Falta el valor en SOL (0.05 a 10)."
                    if not (0.05 <= v <= 10):
                        return ("Fuera de rango: el tope debe estar entre "
                                "0.05 y 10 SOL. No lo cambié.")
                    antes = get_setting(conn, "paper_max_sol", None)
                    set_setting(conn, "paper_max_sol", str(v))
                    _registrar_ajuste(conn,
                                      {"paper_max_sol": (antes, str(v))})
                    return (f"🧪 Tope del paper: {v:g} SOL por señal "
                            f"(antes: "
                            f"{antes if antes is not None else '1 por defecto'})"
                            f".")
                return ("Acción de paper no reconocida: usa encender, "
                        "apagar o max_sol.")
            finally:
                conn.close()
        if tool == "deshacer_ajuste":
            conn = get_conn()
            try:
                return _deshacer_ultimo(conn)
            finally:
                conn.close()
        if tool == "cambiar_umbral_senal":
            from db import get_setting as _gs, set_setting
            v_raw = float(args.get("valor", 0))
            if v_raw < 0:
                # (Ola 15 - M1) Volver a AUTOMÁTICO también se registra:
                # antes no era deshacible y un "deshacer" posterior
                # revertía un ajuste más viejo con resultado confuso.
                conn = get_conn()
                try:
                    _man_antes = _gs(conn, "umbral_manual", None)
                    if (_man_antes or "0") == "0":
                        return ("El umbral YA estaba en modo automático. "
                                "No cambié nada.")
                    _registrar_ajuste(conn,
                                      {"umbral_manual": (_man_antes, "0")})
                    set_setting(conn, "umbral_manual", "0")
                finally:
                    conn.close()
                return ("🎚️ Auto-ajuste del umbral reactivado: volverá a "
                        "optimizarse solo con el historial medido.")
            v = max(0, min(100, v_raw))
            conn = get_conn()
            try:
                _antes_raw = _gs(conn, "min_signal_score", None)
                _man_antes = _gs(conn, "umbral_manual", None)
                try:
                    _antes = float(_antes_raw or 0)
                except (TypeError, ValueError):
                    _antes = None
                if _antes == v and (_man_antes or "0") == "1":
                    return (f"El umbral YA estaba en {v:.0f}/100 (modo "
                            f"manual). No cambié nada.")
                # Las DOS claves que toca este ajuste, juntas.
                _registrar_ajuste(conn, {
                    "min_signal_score": (_antes_raw, str(v)),
                    "umbral_manual": (_man_antes, "1")})
                set_setting(conn, "min_signal_score", v)
                # El ajuste MANUAL manda (19/8): sin esta marca, el auto-
                # ajustador de signal_tracker lo pisaba a los 15 minutos.
                set_setting(conn, "umbral_manual", "1")
            finally:
                conn.close()
            return (f"🎯 Umbral fijado en {v:.0f}/100 (modo manual: el "
                    f"auto-ajuste queda apagado; di «umbral automático» "
                    f"para reactivarlo). Señales de compra con score "
                    f"menor quedarán silenciadas.")
    except Exception as e:
        return f"Error ejecutando la acción: {e}"
    return "Acción desconocida."
