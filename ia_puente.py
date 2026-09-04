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

REGLA DE LA CASA (19/8, auditoria): ningun otro modulo hace requests a
"chat/completions" — el unico camino a la IA es este archivo. La ultima
vez que un modulo se salto el puente (decidir_salida), se quedo sin el
arreglo del modelo pensante y su experimento midio vacio una semana.
Excepcion documentada y unica: ai_agent.py llama directo porque necesita
`tools` (formato OpenAI) que el puente aun no ofrece — ya tiene su
propio reintento sin tope; cuando el puente soporte tools, migra aqui.

Para decisiones con menu cerrado usa completar_json(): valida la
respuesta contra el menu y devuelve el MOTIVO cuando falla, para que el
llamador lo persista — un veredicto invalido deja de ser indistinguible
de "IA apagada".
"""

import json
import os
import re
import threading

import requests

# (19-AA, auditoria M6) Bandera POR HILO: la ultima llamada a la local
# fallo porque estaba OCUPADA (conecto, pero no contesto a tiempo). Con
# `local_primero` eso NO debe caer a la nube de pago: el modelo esta
# cargado y trabajando (el analista lo tiene 30-90 s por billetera), y el
# tope diario de 300 no protegia justo cuando habia carga. Apagada (no
# conecta) sigue cayendo a la nube.
_HILO = threading.local()

NUBE_URL = "https://api.anthropic.com/v1/messages"
NUBE_MODELO = "claude-haiku-4-5-20251001"

# ¿El modelo local es PENSANTE (ignora /no_think y enable_thinking)?
# None = aun no leido de settings. Se aprende una vez EN LA VIDA: queda
# persistido en settings ("modelo_pensante") porque antes era una global
# de modulo y el supervisor local reinicia el proceso con cada commit —
# cada reinicio re-quemaba un primer intento condenado (auditoria 19/8).
_MODELO_PENSANTE = None

_RE_THINK = re.compile(r"<think>.*?</think>", re.S)


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


def _sin_think(t: str) -> str:
    """Quita los bloques <think>…</think>. Un <think> sin cerrar (el
    modelo se quedo sin tokens a mitad del razonamiento) es TODO
    razonamiento: cuenta como vacio."""
    if not t:
        return ""
    limpio = _RE_THINK.sub("", t)
    s = limpio.strip()
    if s.startswith("<think>"):
        return ""
    return s


def _pensante(conn=None) -> bool:
    global _MODELO_PENSANTE
    if _MODELO_PENSANTE is None:
        _MODELO_PENSANTE = str(
            _setting("modelo_pensante", "0", conn) or "0") == "1"
    return _MODELO_PENSANTE


def _aprender_pensante(conn=None) -> None:
    global _MODELO_PENSANTE
    if _MODELO_PENSANTE is True:
        return
    _MODELO_PENSANTE = True
    print("· IA local: modelo PENSANTE detectado — de aqui en adelante "
          "todas las llamadas van sin tope (anotado en settings)")
    try:
        from db import set_setting
        if conn is not None:
            set_setting(conn, "modelo_pensante", "1")
        else:
            from db import get_conn
            c = get_conn()
            try:
                set_setting(c, "modelo_pensante", "1")
            finally:
                c.close()
    except Exception as e:
        print(f"· No pude persistir modelo_pensante: {e}")


def _local(prompt: str, system: str | None, max_tokens: int,
           timeout: int, conn=None, _reintento: bool = False,
           paciencia: bool = False) -> str | None:
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
        # razonamiento en los modelos que lo honran; a los que lo ignoran
        # los cubre el reintento SIN tope (y modelo_pensante lo recuerda).
        msgs = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": prompt + "\n/no_think"}]
        cuerpo = {"model": modelo, "temperature": 0.3, "messages": msgs,
                  # Apagado "de verdad" del razonamiento via plantilla de
                  # chat; los servidores que no lo entienden lo ignoran.
                  "chat_template_kwargs": {"enable_thinking": False}}
        if not _reintento and not _pensante(conn):
            cuerpo["max_tokens"] = max_tokens + 200
        # En el reintento va SIN tope de tokens (19/8): el modelo pensante
        # razona lo que necesite y termina solo; el limite real que nos
        # protege es el timeout, no el contador.
        _HILO.ocupada = False
        try:
            r = requests.post(
                f"{url}/v1/chat/completions", json=cuerpo, timeout=timeout)
        except requests.exceptions.ReadTimeout as e:
            _HILO.ocupada = True
            print(f"· IA local OCUPADA (no contestó en {timeout} s): "
                  f"no se pasa a la nube ({str(e)[:80]})")
            return None
        if r.status_code >= 400:
            print(f"· IA local HTTP {r.status_code}: {r.text[:200]}")
            return None
        eleccion = r.json()["choices"][0]
        bruto = ((eleccion.get("message") or {}).get("content") or "")
        # El razonamiento NO es respuesta (auditoria 19/8): una salida que
        # es puro <think> contaba como texto valido y viajaba cruda hasta
        # Telegram. Se limpia ANTES del chequeo de vacio, para que tambien
        # dispare el reintento.
        texto = _sin_think(bruto)
        if not texto:
            fin = eleccion.get("finish_reason")
            # Reintento anti-razonamiento. La paciencia la declara el
            # LLAMADOR (paciencia=True: decisiones en hilos de fondo);
            # timeout >= 60 se mantiene como señal implicita para los
            # jobs periodicos que ya llamaban asi.
            if not _reintento and (paciencia or timeout >= 60):
                if fin == "length":
                    _aprender_pensante(conn)
                print(f"· IA local vacia (finish={fin}); "
                      "reintento SIN tope de tokens")
                return _local(prompt, system, max_tokens,
                              max(timeout, 90), conn, _reintento=True,
                              paciencia=paciencia)
            print(f"· IA local respondio VACIO (finish={fin})")
            return None
        return texto
    except Exception as e:
        print(f"· IA local no disponible: {e}")
        return None


def _nube(prompt: str, system: str | None, max_tokens: int,
          timeout: int, conn=None, paciencia: bool = False) -> str | None:
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
                 conn=None, paciencia: bool = False
                 ) -> tuple[str | None, str | None]:
    """Texto de IA segun el proveedor configurado. None si nadie pudo.

    Devuelve (texto, proveedor) — el proveedor viaja en el RETORNO, no
    en una global: las globales entre hilos mezclaban al webhook con los
    jobs periodicos (hallazgo v3). `conn` opcional: quien ya tiene
    conexion la presta y aqui no se abre ninguna.

    `paciencia=True` autoriza el reintento sin tope contra el modelo
    pensante aunque el timeout sea corto — para decisiones que corren en
    hilos de fondo y prefieren respuesta lenta a respuesta vacia."""
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
            texto = intento(prompt, system, max_tokens, timeout, conn,
                            paciencia=paciencia)
            if texto:
                return texto, nombre
            if nombre == "local" and getattr(_HILO, "ocupada", False):
                return None, None       # (19-AA) ocupada ≠ apagada
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


def _candidatos_json(t: str) -> list[str]:
    """Substrings {…} balanceados de nivel superior, en orden de
    aparicion. Respeta llaves dentro de strings JSON."""
    cands: list[str] = []
    prof, ini = 0, -1
    en_str = esc = False
    for i, ch in enumerate(t):
        if en_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                en_str = False
            continue
        if ch == '"':
            en_str = True
        elif ch == "{":
            if prof == 0:
                ini = i
            prof += 1
        elif ch == "}" and prof > 0:
            prof -= 1
            if prof == 0 and ini >= 0:
                cands.append(t[ini:i + 1])
    return cands


def extraer_json(texto: str) -> dict | None:
    """El dict JSON de la respuesta aunque venga envuelto en texto,
    markdown o razonamiento filtrado.

    v2 (auditoria 19/8): el regex voraz \\{.*\\} abarcaba desde la primera
    llave del razonamiento hasta la ultima de la respuesta y descartaba
    veredictos validos — en vivo, 'copiar' no se registro NUNCA. Ahora:
    fuera <think>, y se prueban los objetos balanceados DESDE EL FINAL
    (la respuesta real viene despues del razonamiento). Solo dicts: una
    lista o un string suelto no son un veredicto."""
    if not texto:
        return None
    t = _sin_think(texto).replace("```json", "").replace("```", "").strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    for cand in reversed(_candidatos_json(t)):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def completar_json(prompt: str, menu: dict, *, system: str | None = None,
                   max_tokens: int = 300, timeout: int = 60, conn=None,
                   paciencia: bool = False
                   ) -> tuple[dict | None, str | None, str]:
    """Decision con menu cerrado. Devuelve (dict, proveedor, motivo).

    `menu` = {campo: opciones_permitidas}; el valor del campo se normaliza
    (minusculas, sin espacios) y DEBE estar en las opciones. Extras utiles
    ("razon", "max_min") viajan tal cual si vienen.

    El motivo distingue los desenlaces para que el llamador los persista:
      "ok"                  → dict valido
      "sin_respuesta"       → la IA no contesto (apagada, timeout, vacia)
      "sin_json"            → contesto pero sin JSON parseable
      "fuera_de_menu:campo" → JSON valido con un valor fuera del menu
    Nunca mas un veredicto perdido indistinguible de IA apagada."""
    texto, prov = completar_ex(prompt, system=system, max_tokens=max_tokens,
                               timeout=timeout, conn=conn,
                               paciencia=paciencia)
    if not texto:
        return None, None, "sin_respuesta"
    v = extraer_json(texto)
    if v is None:
        print(f"· IA sin JSON [{prov}]: {texto[:120]}")
        return None, prov, "sin_json"
    salida: dict = {}
    for campo, permitidas in menu.items():
        val = str(v.get(campo) or "").lower().strip()
        if val not in permitidas:
            print(f"· IA fuera de menu [{prov}]: {campo}={val!r}")
            return None, prov, f"fuera_de_menu:{campo}"
        salida[campo] = val
    for extra in ("razon", "max_min"):
        if extra in v and extra not in salida:
            salida[extra] = v[extra]
    return salida, prov, "ok"


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
