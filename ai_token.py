"""
Veredicto de la IA sobre un TOKEN (no una billetera).

Recibe la ficha de token_check + el contexto de smart-money propio y devuelve
un nivel de riesgo + razón + confianza. Protegido por:
  - caché por mint (no re-consultar el mismo token en X horas),
  - el presupuesto diario de IA (ai_budget); si se agotó → None,
  - si no hay ANTHROPIC_API_KEY → None.

Cuando devuelve None, el llamador usa el Risk Score heurístico como respaldo.
No modifica nada del pipeline existente.
"""

import json
import os
import time

import requests

from token_check import ai_payload

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"


def _cache_horas() -> int:
    try:
        return int(os.getenv("TOKEN_VERDICT_CACHE_H", "6"))
    except ValueError:
        return 6


PROMPT = """Eres un analista de riesgo de tokens en Solana. Con estos datos, juzga si es RAZONABLE invertir o si es demasiado arriesgado. NO das consejo financiero: das una lectura de riesgo objetiva.

FICHA DEL TOKEN (DexScreener + RugCheck):
{ficha}

CONTEXTO DE NUESTRA RED (billeteras rentables que YA hemos verificado y que compraron este token; es una señal fuerte si es alta):
{smart}

APRENDIZAJES ACUMULADOS (patrones de tokens que el usuario marcó buenos/malos; úsalos como criterio si aplican):
{aprendizajes}

Cómo pensar el riesgo:
- Autoridad de mint activa (mint_auth no nula) = el dev puede imprimir más = MUY arriesgado.
- Autoridad de freeze activa = puede congelar ventas = arriesgado.
- LP bloqueado bajo (lp_locked_pct < 50) o nulo = riesgo de rug.
- Concentración alta (top10_pct ≥ 40) = pocos holders controlan el precio.
- Liquidez baja vs market cap = difícil salir sin mover el precio.
- Riesgos de RugCheck (risks) = súmalos.
- Que billeteras de calidad NUESTRAS lo hayan comprado temprano es señal POSITIVA fuerte.

Responde SOLO con JSON válido, sin markdown ni texto extra:
{{"nivel": "seguro" | "riesgo medio" | "riesgo alto", "seguro": true/false, "confianza": 0-100, "razon": "máximo 2 frases en español, concreto sobre lo que viste"}}"""


def _call(prompt: str) -> dict | None:
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 250,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        import re as _re
        m = _re.search(r"\{.*\}", text, flags=_re.S)
        v = json.loads(m.group(0) if m else text)
        if v.get("nivel"):
            return v
    except Exception as e:
        print(f"  · Error IA token: {e}")
    return None


def _cache_get(mint: str):
    try:
        from db import get_conn, get_setting
        conn = get_conn()
        try:
            raw = get_setting(conn, f"tv:{mint}", "")
        finally:
            conn.close()
        if not raw:
            return None
        d = json.loads(raw)
        if time.time() - d.get("ts", 0) > _cache_horas() * 3600:
            return None
        return d.get("v")
    except Exception:
        return None


def _cache_put(mint: str, v: dict):
    try:
        from db import get_conn, set_setting
        conn = get_conn()
        try:
            set_setting(conn, f"tv:{mint}", json.dumps({"ts": time.time(),
                                                        "v": v}))
        finally:
            conn.close()
    except Exception:
        pass


def token_verdict(t: dict, smart_ctx: dict, mint: str) -> dict | None:
    """
    Veredicto IA del token. None si no hay API key, no queda presupuesto,
    o la llamada falla → el llamador cae al Risk Score heurístico.
    """
    if not ANTHROPIC_API_KEY:
        return None

    cached = _cache_get(mint)
    if cached:
        return cached

    # Presupuesto diario de IA
    try:
        from db import get_conn
        from ai_budget import can_call
        conn = get_conn()
        try:
            if not can_call(conn):
                return None
        finally:
            conn.close()
    except Exception:
        pass  # si el módulo de presupuesto falla, no bloquea el veredicto

    try:
        from token_learning import learnings_text
        aprendizajes = learnings_text() or "(aún sin aprendizajes acumulados)"
    except Exception:
        aprendizajes = "(aún sin aprendizajes acumulados)"
    prompt = PROMPT.format(
        ficha=json.dumps(ai_payload(t), ensure_ascii=False),
        smart=json.dumps(smart_ctx, ensure_ascii=False),
        aprendizajes=aprendizajes)
    v = _call(prompt)
    if not v:
        return None
    v.setdefault("modelo", MODEL)

    # Registrar el consumo de presupuesto
    try:
        from db import get_conn
        from ai_budget import record_call
        conn = get_conn()
        try:
            record_call(conn)
        finally:
            conn.close()
    except Exception:
        pass

    _cache_put(mint, v)
    return v
