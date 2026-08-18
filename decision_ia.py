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

TIMEOUT = 15          # la decision es en caliente: si tarda mas, reglas
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
    return m or "qwen"


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
