"""
Puente unico de IA: TODO el texto que antes decidia la API de pago de
Claude sale ahora de la IA LOCAL del dueño (LM Studio + Qwen via tunel).

Decision del 18/8/2026: "no creo que vaya a pagar mas por la API" — la
local es titular. La nube queda como respaldo OPCIONAL por si algun dia
se recargan creditos (setting "ia_proveedor"):

    local_primero  (defecto)  local → si no esta, intenta nube
    local                     SOLO local (ni intenta la nube)
    nube                      nube → si falla, local (el modo viejo)

Todos los modulos que necesiten un texto de IA llaman a completar() y no
saben ni les importa quien respondio. Un solo sitio decide, un solo sitio
se arregla.
"""

import json
import os

import requests

NUBE_URL = "https://api.anthropic.com/v1/messages"
NUBE_MODELO = "claude-haiku-4-5-20251001"


def _setting(key: str, default):
    try:
        from db import get_conn, get_setting
        c = get_conn()
        try:
            return get_setting(c, key, default)
        finally:
            c.close()
    except Exception:
        return default


def _local(prompt: str, system: str | None, max_tokens: int,
           timeout: int) -> str | None:
    try:
        from decision_ia import _url, _modelo
        from db import get_conn
        c = get_conn()
        try:
            url, modelo = _url(c), _modelo(c)
        finally:
            c.close()
        if not url:
            return None
        msgs = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": prompt}]
        r = requests.post(
            f"{url}/v1/chat/completions",
            json={"model": modelo, "temperature": 0.3,
                  "max_tokens": max_tokens, "messages": msgs},
            timeout=timeout)
        if r.status_code >= 400:
            print(f"· IA local HTTP {r.status_code}: {r.text[:200]}")
            return None
        return (r.json()["choices"][0]["message"]["content"] or "").strip() \
            or None
    except Exception as e:
        print(f"· IA local no disponible: {e}")
        return None


def _nube(prompt: str, system: str | None, max_tokens: int,
          timeout: int) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    try:
        body = {"model": NUBE_MODELO, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        r = requests.post(
            NUBE_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body, timeout=timeout)
        if r.status_code >= 400:
            print(f"· IA nube HTTP {r.status_code}: {r.text[:200]}")
            return None
        return "".join(b.get("text", "")
                       for b in r.json().get("content", [])).strip() or None
    except Exception as e:
        print(f"· IA nube no disponible: {e}")
        return None


def completar(prompt: str, system: str | None = None,
              max_tokens: int = 500, timeout: int = 60) -> str | None:
    """Texto de IA segun el proveedor configurado. None si nadie pudo."""
    orden = str(_setting("ia_proveedor", "local_primero") or "local_primero")
    if orden == "nube":
        cadena = (_nube, _local)
    elif orden == "local":
        cadena = (_local,)
    else:                                   # local_primero
        cadena = (_local, _nube)
    for intento in cadena:
        texto = intento(prompt, system, max_tokens, timeout)
        if texto:
            return texto
    return None


def extraer_json(texto: str) -> dict | None:
    """El JSON de la respuesta aunque venga envuelto en texto/markdown."""
    if not texto:
        return None
    t = texto.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def hay_ia() -> bool:
    """¿Hay ALGUN proveedor de IA disponible? Sustituye a los porteros
    `if not ANTHROPIC_API_KEY: return` repartidos por el codigo: con la
    local de titular, no tener clave de nube ya no significa no tener IA."""
    if os.getenv("ANTHROPIC_API_KEY", ""):
        return True
    return bool(os.getenv("LOCAL_AI_URL", "")
                or _setting("local_ai_url", ""))
