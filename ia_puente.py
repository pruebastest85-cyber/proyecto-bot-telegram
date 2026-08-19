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


ultimo_proveedor = None      # "local" | "nube" tras cada completar()


def _setting(key: str, default, conn=None):
    try:
        from db import get_setting
        if conn is not None:
            return get_setting(conn, key, default)
        from db import get_conn
        c = get_conn()
        try:
            return get_setting(c, key, default)
        finally:
            c.close()
    except Exception:
        return default


def _local(prompt: str, system: str | None, max_tokens: int,
           timeout: int, conn=None) -> str | None:
    try:
        from decision_ia import _url, _modelo
        if conn is not None:
            url, modelo = _url(conn), _modelo(conn)
        else:
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
          timeout: int, conn=None) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    # El presupuesto diario SOLO gobierna la nube: la local es gratis y
    # frenar a la local con el contador de la nube era un contrasentido
    # (hallazgo de revision del 18/8).
    try:
        from ai_budget import budget_left
        from db import get_conn as _gc
        _cb = conn if conn is not None else _gc()
        try:
            if budget_left(_cb) <= 0:
                return None       # presupuesto de NUBE agotado: no gastar
        finally:
            if conn is None:
                _cb.close()
    except Exception:
        pass
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
              max_tokens: int = 500, timeout: int = 60,
              conn=None) -> str | None:
    """Texto de IA segun el proveedor configurado. None si nadie pudo.

    `conn` opcional: quien ya tiene conexion (p. ej. el hilo del webhook)
    la presta y este modulo no abre ninguna — sin ella, se abre UNA sola
    para toda la llamada (antes eran dos por llamada)."""
    global ultimo_proveedor
    propia = None
    if conn is None:
        try:
            from db import get_conn
            propia = conn = get_conn()
        except Exception:
            conn = None
    try:
        orden = str(_setting("ia_proveedor", "local_primero", conn)
                    or "local_primero")
        if orden == "nube":
            cadena = (("nube", _nube), ("local", _local))
        elif orden == "local":
            cadena = (("local", _local),)
        else:                               # local_primero
            cadena = (("local", _local), ("nube", _nube))
        for nombre, intento in cadena:
            texto = intento(prompt, system, max_tokens, timeout, conn)
            if texto:
                ultimo_proveedor = nombre
                return texto
        ultimo_proveedor = None
        return None
    finally:
        if propia is not None:
            propia.close()


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
    """¿Hay algun proveedor QUE LA CADENA ACTUAL pueda usar? Coherente
    con completar(): en modo "local" la clave de nube no cuenta, y en
    modo "nube" lo local solo cuenta como respaldo."""
    orden = str(_setting("ia_proveedor", "local_primero") or "local_primero")
    local_ok = bool(os.getenv("LOCAL_AI_URL", "")
                    or _setting("local_ai_url", ""))
    nube_ok = bool(os.getenv("ANTHROPIC_API_KEY", ""))
    if orden == "local":
        return local_ok
    return local_ok or nube_ok
