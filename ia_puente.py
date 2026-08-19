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
           timeout: int, conn=None, _reintento: bool = False) -> str | None:
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
        # Qwen 3.x es un modelo PENSANTE: sin el interruptor /no_think se
        # gasta el max_tokens razonando y el texto visible llega vacio con
        # HTTP 200 — el "Sin IA" silencioso del 18/8. /no_think apaga el
        # razonamiento (respuesta directa y rapida) y el colchon de tokens
        # cubre a los modelos que lo ignoren.
        msgs = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": prompt + "\n/no_think"}]
        cuerpo = {"model": modelo, "temperature": 0.3, "messages": msgs,
                  # Apagado "de verdad" del razonamiento via plantilla de
                  # chat; los servidores que no lo entienden lo ignoran.
                  "chat_template_kwargs": {"enable_thinking": False}}
        if not _reintento:
            cuerpo["max_tokens"] = max_tokens + 200
        # En el reintento va SIN tope de tokens (19/8): el modelo pensante
        # razona lo que necesite y termina solo; el limite real que nos
        # protege es el timeout, no el contador.
        r = requests.post(
            f"{url}/v1/chat/completions", json=cuerpo, timeout=timeout)
        if r.status_code >= 400:
            print(f"· IA local HTTP {r.status_code}: {r.text[:200]}")
            return None
        eleccion = r.json()["choices"][0]
        texto = ((eleccion.get("message") or {}).get("content") or "").strip()
        if not texto:
            fin = eleccion.get("finish_reason")
            # Variantes pensantes de Qwen que IGNORAN /no_think (18/8, se
            # vio en vivo: reasoning_tokens 499 de 500): un solo reintento
            # con presupuesto grande deja al modelo terminar de pensar y
            # escribir. Lento pero con respuesta > rapido pero vacio.
            # Solo reintentar cuando el llamador ya venia con paciencia
            # (jobs periodicos): el hilo del webhook llama con timeout=25
            # y no debe quedarse 2 minutos esperando (hallazgo 18/8).
            if fin == "length" and not _reintento and timeout >= 60:
                print("· IA local: el razonamiento se comio el tope; "
                      "reintento SIN tope de tokens")
                return _local(prompt, system, max_tokens,
                              max(timeout, 120), conn, _reintento=True)
            print(f"· IA local respondio VACIO (finish={fin})")
            return None
        return texto
    except Exception as e:
        print(f"· IA local no disponible: {e}")
        return None


def _nube(prompt: str, system: str | None, max_tokens: int,
          timeout: int, conn=None) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    # El presupuesto diario SOLO gobierna la nube (la local es gratis), y
    # es la PROPIA nube quien se cuenta al terminar bien: un solo sitio,
    # sin depender de que cada llamador recuerde registrar (v3, 18/8).
    # v4 (18/8): si el presupuesto NO se puede leer, NO se llama — mejor
    # quedarse sin nube que gastar dolares sin contarlos. Y un solo
    # try/finally: antes el "agotado" retornaba sin cerrar la conexion
    # (una filtrada por intento, justo en el caso que mas se repite).
    try:
        from ai_budget import budget_left, record_call
    except Exception as e:
        print(f"· Nube sin presupuesto legible ({e}): no se gasta")
        return None
    _cb = None
    try:
        if conn is None:
            from db import get_conn as _gc
            _cb = _gc()
        _bconn = conn if conn is not None else _cb
        if budget_left(_bconn) <= 0:
            return None           # presupuesto de NUBE agotado: no gastar
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
        texto = "".join(b.get("text", "")
                        for b in r.json().get("content", [])).strip() or None
        if texto:
            try:
                record_call(_bconn)
            except Exception:
                pass
        return texto
    except Exception as e:
        print(f"· IA nube no disponible: {e}")
        return None
    finally:
        if _cb is not None:
            _cb.close()


def completar_ex(prompt: str, system: str | None = None,
                 max_tokens: int = 500, timeout: int = 60,
                 conn=None) -> tuple[str | None, str | None]:
    """Texto de IA segun el proveedor configurado. None si nadie pudo.

    Devuelve (texto, proveedor) — el proveedor viaja en el RETORNO, no
    en una global: las globales entre hilos mezclaban al webhook con los
    jobs periodicos (hallazgo v3). `conn` opcional: quien ya tiene
    conexion la presta y aqui no se abre ninguna."""
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
                return texto, nombre
        return None, None
    finally:
        if propia is not None:
            propia.close()


def completar(prompt: str, system: str | None = None,
              max_tokens: int = 500, timeout: int = 60,
              conn=None) -> str | None:
    """Version simple: solo el texto (la mayoria no necesita saber quien
    respondio)."""
    return completar_ex(prompt, system, max_tokens, timeout, conn)[0]


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
