"""
Fase 2 — Monitoreo en tiempo real de billeteras ⭐.

v5: detecta COMPRAS y VENTAS; cada señal incluye alias IA y PnL de la
billetera, link a DexScreener y redes sociales del token.
Anti-duplicados: un candado global evita que dos entregas simultáneas
del mismo webhook de Helius alerten la misma operación dos veces.

Piezas:
  1. Servidor de webhooks (Flask): recibe de Helius cada transacción
     de las billeteras rastreadas, al instante.
  2. Sincronizador: registra/actualiza el webhook en Helius con la
     lista actual de billeteras ⭐ (se llama al final de cada ciclo).
  3. Motor de señales: cuando una ⭐ COMPRA o VENDE un token →
     RugCheck + DexScreener + consenso + veredicto IA → alerta TG.

Variables de entorno usadas:
  PUBLIC_URL   → dominio público de Railway
  PORT         → lo inyecta Railway automáticamente
  + las ya existentes (HELIUS_API_KEY, TELEGRAM_*, ANTHROPIC_API_KEY)
"""

import json
import os
import threading
import time

import requests
from flask import Flask, request, jsonify

import config
from db import get_conn, get_setting, top_addresses
from token_check import analyze_token, format_token_block, ai_payload
from signal_score import compute_signal_score

LAMPORTS = 1_000_000_000
LAST_HOOK_TS = None   # última vez que Helius nos mandó algo (watchdog)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PUBLIC_URL = PUBLIC_URL.removeprefix("https://").removeprefix("http://")
PORT = int(os.getenv("PORT", "8080"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Candado anti-duplicados: serializa el "quién registra primero la señal"
# para que dos hilos (entregas simultáneas del mismo webhook) no alerten
# la misma operación dos veces.
_SIGNAL_LOCK = threading.Lock()

# Candados FINOS por mint (Ola 5, auditoria 19/8 - M4): dos transacciones
# del mismo token en el mismo bloque llegan como dos hilos; apply_buy /
# apply_sell hacen leer-modificar-escribir sobre positions (una compra se
# perdia) y el "una posicion por token" de open_trade es SELECT-then-
# INSERT (dos posiciones del mismo mint). Serializar por mint arregla
# ambos sin frenar a los demas tokens.
_MINT_LOCKS: dict = {}
_MINT_LOCKS_GUARD = threading.Lock()
# Los ultimos mints cuyo candado se ENTREGO. La purga no los toca: ver el
# comentario de `_lock_mint`. Tope fijo, asi que no crece.
from collections import deque as _deque
_MINT_RECIENTES = _deque(maxlen=256)


def _lock_mint(mint: str) -> threading.Lock:
    """Candado por token. Serializa el leer-modificar-escribir de
    `positions`, de la copia simulada y (Ola 18-G) del registro de
    llegadas del motor predictivo.

    (Ola 18-G) Antes, al pasar de 4096 se hacia `clear()` del diccionario
    entero. Si en ese momento un hilo TENIA TOMADO el candado de un mint,
    la siguiente peticion de ese mismo mint devolvia un candado NUEVO y
    distinto: los dos hilos entraban a la vez y volvia la perdida de
    llegadas, sin una linea de log. Ahora se sueltan solo los que NADIE
    tiene tomados, y de los mas antiguos: el diccionario sigue acotado y
    la exclusion mutua no se rompe.
    """
    with _MINT_LOCKS_GUARD:
        if len(_MINT_LOCKS) > 4096:
            # (Ola 18-G, 3ª vuelta) `c.locked()` no basta: el candado se
            # ENTREGA aqui y el llamador lo toma unos bytecodes despues,
            # asi que en esa ventana figura como libre y la purga podia
            # sacarlo — el siguiente que pidiera ese mint recibiria un
            # objeto DISTINTO y entrarian los dos a la vez. Por eso se
            # protegen ademas los ULTIMOS entregados. La ventana entre
            # entregar y tomar dura unos bytecodes, y en ese rato como
            # mucho pasan por aqui tantos mints como hilos haya: 256 de
            # margen sobra. El tope es fijo, asi que no crece.
            protegidos = set(_MINT_RECIENTES)
            for k in [k for k, c in list(_MINT_LOCKS.items())
                      if not c.locked() and k not in protegidos][:2048]:
                _MINT_LOCKS.pop(k, None)
        candado = _MINT_LOCKS.setdefault(mint, threading.Lock())
        _MINT_RECIENTES.append(mint)
        return candado

HELIUS_WEBHOOKS = "https://api.helius.xyz/v0/webhooks"

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
MIN_SIGNAL_SOL = 0.3
# Umbral APARTE para ventas (Ola 6, auditoria 19/8 - M1): filtrar ventas
# por lo RECIBIDO ocultaba las salidas perdedoras — una ⭐ que compro con
# 0.5 SOL y sale a -50% recibe 0.25 y no generaba señal: positions no se
# decrementaba (la proxima compra salia como "ACUMULANDO" falso), el
# paper no seguia las salidas malas (si las buenas) y la deriva
# post-venta solo veia ventas rentables. Sesgo optimista en todo.
MIN_SIGNAL_SOL_VENTA = 0.05
CONSENSUS_WINDOW_MIN = 45


def tg_send(text: str, buttons: list | None = None) -> bool:
    """Envía mensaje al admin vía HTTP API (seguro desde cualquier hilo).
    buttons: lista de filas [[(texto, callback_data), …], …].

    (Ola 17-L, 25/8) Devuelve True SOLO si Telegram aceptó el mensaje.
    Antes no devolvía nada y un rechazo (400 por Markdown roto, 429 por
    exceso de ritmo) se quedaba en un `print`: la alerta se perdía, no
    quedaba rastro en `/errores`, y quien llamaba marcaba `alerted=1`
    igual — o sea que la base decía "enviada" sin que hubiera llegado.
    Es la misma clase de fallo silencioso de la ola 17-I."""
    if not (BOT_TOKEN and ADMIN_ID):
        return False
    payload = {"chat_id": int(ADMIN_ID), "text": text,
               "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": tx, "callback_data": cb} for tx, cb in fila]
            for fila in buttons]}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15)
        if r.ok:
            return True
        # Markdown roto (simbolos con *_`[ ) u otro 400: reintentar en
        # texto plano para NO perder la alerta en silencio.
        payload.pop("parse_mode", None)
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=15)
        if r.ok:
            return True
        print(f"· Alerta TG rechazada ({r.status_code}): "
              f"{r.text[:200]}")
        try:
            from errores import record
            record("telegram_send",
                   RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}"),
                   "rechazado tras reintento en texto plano")
        except Exception:
            pass
        return False
    except requests.RequestException as e:
        print(f"· No se pudo enviar alerta TG: {e}")
        try:
            from errores import record
            record("telegram_send", e)
        except Exception:
            pass
        return False


def tg_send_photo(photo_bytes: bytes, caption: str = ""):
    """Envía una foto con caption (Markdown) al admin — para tarjetas."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": int(ADMIN_ID), "caption": caption[:1000],
                  "parse_mode": "Markdown"},
            files={"photo": ("card.jpg", photo_bytes, "image/jpeg")},
            timeout=25)
        if not r.ok:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": int(ADMIN_ID), "caption": caption[:1000]},
                files={"photo": ("card.jpg", photo_bytes, "image/jpeg")},
                timeout=25)
            if not r.ok:
                print(f"· Foto TG rechazada ({r.status_code}): "
                      f"{r.text[:200]}")
    except requests.RequestException as e:
        print(f"· No se pudo enviar foto TG: {e}")


def tracked_addresses(conn=None) -> list[str]:
    """Si se pasa `conn` se reutiliza (y NO se cierra: es de quien la abrio).
    Sin `conn` abre y cierra la suya, como siempre."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT address FROM wallets WHERE is_tracked=1").fetchall()
    finally:
        if propia:
            conn.close()
    return [r["address"] for r in rows]


def watch_addresses(conn=None) -> list[str]:
    """⭐ + candidatas prometedoras (liga de ascenso): las candidatas se
    monitorean en silencio para construir su track record real antes de
    darles la estrella.

    Igual que tracked_addresses: `conn` opcional para no abrir una conexion
    nueva cuando quien llama ya tiene una."""
    propia = conn is None
    if propia:
        conn = get_conn()
    stars = [r["address"] for r in conn.execute(
        "SELECT address FROM wallets WHERE is_tracked=1").fetchall()]
    hace_6h = int(time.time()) - 6 * 3600
    cands = [r["address"] for r in conn.execute(
        """SELECT address FROM wallets w
           WHERE is_tracked=0 AND is_bot=0 AND winning_tokens_count >= 2
             AND (SELECT COUNT(*) FROM signals s
                  WHERE s.wallet = w.address AND s.ts >= ?) <= 30
           ORDER BY score DESC LIMIT 40""", (hace_6h,)).fetchall()]
    # Billeteras que ya NO son ⭐ ni candidatas pero tienen una posición
    # simulada ABIERTA: hay que seguir viéndolas para poder cerrarla con su
    # venta real. Si salen de la vigilancia, esa posición acabaría cerrando
    # por TP/SL/tiempo y la simulación dejaría de medir lo que pretendía.
    try:
        huerfanas = [r["wallet"] for r in conn.execute(
            "SELECT DISTINCT wallet FROM paper_trades "
            "WHERE status='abierta'").fetchall()]
    except Exception:
        huerfanas = []            # tabla aún sin crear
    # (Ola 12b, 21/8) Devs de tokens con posición abierta: entran a la
    # VIGILANCIA EN TIEMPO REAL. El sondeo de 15 min llegaba tarde por
    # definición — cuando el dev vende, el desplome es inmediato. Con el
    # dev suscrito en LaserStream la alerta llega en segundos.
    try:
        devs = [r["dev_wallet"] for r in conn.execute(
            "SELECT DISTINCT dev_wallet FROM paper_trades "
            "WHERE status='abierta' AND dev_wallet IS NOT NULL").fetchall()]
    except Exception:
        devs = []                 # tabla/columna aún sin crear
    if propia:
        conn.close()
    fuera = [c for c in cands if c not in stars]
    extra = [h for h in huerfanas if h not in stars and h not in fuera]
    ya = set(stars) | set(fuera) | set(extra)
    extra_devs = [d for d in devs if d not in ya]
    return stars + fuera + extra + extra_devs


def _guardar_huella(huella: str) -> None:
    """Recuerda qué lista de ⭐ quedó sincronizada, para no repetir la llamada."""
    try:
        from db import get_conn, set_setting
        _c = get_conn()
        try:
            set_setting(_c, "webhook_huella", huella)
            set_setting(_c, "webhook_sync_ts", time.time())
        finally:
            _c.close()
    except Exception:
        pass


def sync_helius_webhook(forzar: bool = False) -> str:
    """
    Crea o actualiza el webhook de Helius con las billeteras ⭐.

    AHORRO: cada gestión de webhook cuesta 100 CRÉDITOS (consultar + actualizar
    = 200). Este job corría cada 30 min aunque la lista de ⭐ no hubiera
    cambiado, y eso era ~9% de todo el consumo de Helius. Ahora se compara una
    huella de la lista y solo se llama a la API si cambió de verdad. Cada 24 h
    se fuerza igualmente, por si el webhook se borró por fuera.
    """
    if not PUBLIC_URL:
        return "PUBLIC_URL no configurada; webhook no sincronizado"
    invalidar_vigiladas()      # la lista va a cambiar: que no quede cacheada
    addrs = watch_addresses()
    if not addrs:
        return "Sin billeteras ⭐ aún; nada que monitorear"

    # ¿Cambió algo desde la última sincronización?
    import hashlib
    huella = hashlib.sha1(",".join(sorted(addrs)).encode()).hexdigest()[:16]
    if not forzar:
        try:
            from db import get_conn, get_setting
            _c = get_conn()
            try:
                previa = get_setting(_c, "webhook_huella", "") or ""
                ts_prev = float(get_setting(_c, "webhook_sync_ts", 0) or 0)
            finally:
                _c.close()
            if previa == huella and (time.time() - ts_prev) < 86400:
                return (f"Webhook sin cambios ({len(addrs)} billeteras); "
                        "no se gastan créditos")
        except Exception:
            pass

    hook_url = f"https://{PUBLIC_URL}/helius"
    params = {"api-key": config.HELIUS_API_KEY}
    body = {
        "webhookURL": hook_url,
        "transactionTypes": ["ANY"],
        "accountAddresses": addrs,
        "webhookType": "enhanced",
        "authHeader": config.HELIUS_API_KEY,
    }
    try:
        r = requests.get(HELIUS_WEBHOOKS, params=params, timeout=20)
        r.raise_for_status()
        existing = [w for w in r.json() if w.get("webhookURL") == hook_url]
        if existing:
            wid = existing[0]["webhookID"]
            r = requests.put(f"{HELIUS_WEBHOOKS}/{wid}", params=params,
                             json=body, timeout=20)
            r.raise_for_status()
            _guardar_huella(huella)
            return f"Webhook actualizado: {len(addrs)} billeteras vigiladas"
        r = requests.post(HELIUS_WEBHOOKS, params=params, json=body,
                          timeout=20)
        r.raise_for_status()
        _guardar_huella(huella)
        return f"Webhook creado: {len(addrs)} billeteras vigiladas"
    except requests.RequestException as e:
        return f"Error sincronizando webhook: {e}"


MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_SMART = os.getenv("AI_SMART_MODEL", "claude-sonnet-5")


# ── (Ola 18-E) El veredicto de la IA, saneado ─────────────────────────
_VEREDICTOS = ("entrar", "precaucion", "evitar", "salir")
# La IA responde en español y a veces con tilde ("precaución"), o con la
# palabra dentro de una frase. Se normaliza sin depender de acentos.
_SIN_TILDE = str.maketrans("áéíóúÁÉÍÓÚüÜ", "aeiouAEIOUuU")


def _sanear_veredicto(v) -> dict:
    """Un dict con `veredicto` (uno de `_VEREDICTOS`, o None) y `razon`
    (texto, nunca None).

    POR QUE EXISTE: `extraer_json` devuelve lo que haya contestado el
    modelo. Si contestaba `{"veredicto": null}` —o una lista, o un texto—
    la linea del mensaje hacia `.get("veredicto").upper()` y reventaba con
    AttributeError. Eso ocurre DENTRO de `_proc`, fuera de todo `try`
    util: el reintento de `process_transactions` solo cubre "database is
    locked", asi que **se perdia el lote entero del webhook**, no solo esa
    señal. Una respuesta rara de la IA no puede tirar la ingesta.
    """
    if not isinstance(v, dict):
        return {"veredicto": None, "razon": ""}
    crudo = v.get("veredicto")
    ver = None
    if crudo is not None and not isinstance(crudo, (dict, list)):
        txt = str(crudo).strip().lower().translate(_SIN_TILDE)
        if txt in _VEREDICTOS:
            ver = txt
        else:
            # "evitar: baja liquidez" → la PRIMERA palabra, y solo esa.
            # NADA de buscar la palabra dentro de la frase: "no entrar"
            # contiene "entrar" y saldria como 🟢 ENTRAR, que es lo
            # contrario de lo que dijo la IA — y ademas se grabaria asi en
            # `signals.verdict`, que es lo que lee el aprendizaje semanal
            # ("¿los veredictos 'entrar' acertaron mas que los 'evitar'?").
            # Si no encaja la primera palabra, se prefiere no tener
            # veredicto (⚪) a tener uno al reves.
            primera = ""
            for ch in txt:
                if ch.isalpha():
                    primera += ch
                elif primera:
                    break
            if primera in _VEREDICTOS:
                ver = primera
    razon = v.get("razon")
    if razon is None or isinstance(razon, (dict, list)):
        razon = ""
    # La razon la escribe la IA y va dentro de `_..._` en la alerta: sin
    # limpiarla, un `*` o un `_` rompe el Markdown del mensaje entero.
    razon = str(razon).strip()[:400]
    for _c, _r in (("*", ""), ("_", " "), ("`", ""), ("[", "("), ("]", ")")):
        razon = razon.replace(_c, _r)
    return {"veredicto": ver, "razon": razon}


def _ai_signal_verdict(payload: dict, smart: bool = False,
                       conn=None) -> dict | None:
    # (None si no hay IA o no contesto; si contesta, SIEMPRE un dict
    # saneado — ver `_sanear_veredicto`.)
    """Veredicto IA de la señal via el puente (18/8/2026): la LOCAL es
    titular; `smart` queda como pista historica (la nube escalaba de
    modelo, el puente ya no distingue). `conn` prestada del hilo que
    llama para no abrir conexiones extra; timeout 25 s: corre en el
    hilo del webhook DESPUES de todos los filtros (pocas veces al dia),
    pero sigue siendo un maximo, no un objetivo."""
    from ia_puente import hay_ia, completar, extraer_json
    if not hay_ia():
        return None
    prompt = (
        "Eres analista de riesgo en memecoins de Solana. Una billetera "
        "rastreada (clasificada como trader/sniper rentable) acaba de "
        "operar este token; el campo 'accion' dice si fue compra o venta. "
        "El campo 'track_record_billetera' resume cómo les fue a sus "
        "señales anteriores (dato clave). Si es venta, evalúa si conviene "
        "salir. Datos:\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
        'Responde SOLO JSON: {"veredicto":"entrar"|"precaucion"|"evitar"|"salir",'
        '"razon":"máx 2 frases en español"}')
    text = completar(prompt, max_tokens=200, timeout=25, conn=conn)
    if not text:
        return None
    return _sanear_veredicto(extraer_json(text))
def _wallet_sol_delta(tx: dict, wallet: str) -> float:
    """Cambio neto de SOL de la billetera en esta tx (negativo = gastó)."""
    for acc in (tx.get("accountData") or []):
        if acc.get("account") == wallet:
            return int(acc.get("nativeBalanceChange", 0)) / LAMPORTS
    return 0.0


def _detect_trade(tx: dict, tracked: set[str]) -> dict | None:
    """¿Alguna billetera rastreada COMPRÓ o VENDIÓ un token en esta tx?"""
    if tx.get("transactionError"):
        return None
    for t in (tx.get("tokenTransfers") or []):
        mint = t.get("mint")
        if not mint or mint in IGNORED_MINTS:
            continue

        buyer = t.get("toUserAccount")
        if buyer in tracked:
            delta = _wallet_sol_delta(tx, buyer)
            if delta < 0 and abs(delta) >= MIN_SIGNAL_SOL:
                return {"wallet": buyer, "mint": mint, "sol": abs(delta),
                        "side": "compra",
                        "tokens": _tok_total(tx, mint, buyer, "in"),
                        "signature": tx.get("signature", ""),
                        "ts": tx.get("timestamp") or int(time.time())}

        seller = t.get("fromUserAccount")
        if seller in tracked:
            delta = _wallet_sol_delta(tx, seller)
            if delta > 0 and delta >= MIN_SIGNAL_SOL_VENTA:
                return {"wallet": seller, "mint": mint, "sol": delta,
                        "side": "venta",
                        "tokens": _tok_total(tx, mint, seller, "out"),
                        "signature": tx.get("signature", ""),
                        "ts": tx.get("timestamp") or int(time.time())}
    return None


def _wget(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _plan_salida(w) -> str:
    """
    Sugerencia de salida basada en la CONDUCTA REAL de esa billetera:
    cuánto suele aguantar (hold_median_min) y su ROI mediano por operación.
    Copiar a alguien sin plan de salida es media estrategia; esto da el
    marco. NO es consejo financiero ni una garantía: es lo que esa
    billetera hace habitualmente.
    """
    try:
        hold = _wget(w, "hold_median_min")
        roi = _wget(w, "roi_median")
    except Exception:
        return ""
    if hold is None and roi is None:
        return ""
    partes = []
    if roi is not None:
        try:
            roi = float(roi)
            if roi > 0:
                # Objetivo algo por debajo de su mediana: salir antes que
                # la multitud es más realista que perseguir su máximo.
                tp = max(15, round(roi * 0.8))
                sl = max(15, min(40, round(tp / 2)))
                partes.append(f"objetivo *+{tp:.0f}%* · stop *-{sl:.0f}%*")
        except (TypeError, ValueError):
            pass
    if hold is not None:
        try:
            hold = float(hold)
            if hold >= 60:
                partes.append(f"suele aguantar ~{hold / 60:.1f} h")
            elif hold > 0:
                partes.append(f"suele aguantar ~{hold:.0f} min")
        except (TypeError, ValueError):
            pass
    if not partes:
        return ""
    return "\n🎯 Plan sugerido: " + "  ·  ".join(partes)


def _bar(score, width=10) -> str:
    """Barra visual del score, ej. 63/100 -> ▰▰▰▰▰▰▱▱▱▱."""
    try:
        s = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        s = 0.0
    filled = int(round(s / 100 * width))
    return "▰" * filled + "▱" * (width - filled)


def _tok_amount(transfer: dict) -> float:
    """Cantidad de tokens (UI amount) de un tokenTransfer de Helius."""
    v = transfer.get("tokenAmount")
    try:
        return abs(float(v)) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _tok_total(tx: dict, mint: str, wallet: str, direction: str) -> float:
    """Suma TODOS los transfers del mint para la wallet en la tx: las rutas
    partidas de Jupiter generan varios transfers del mismo mint y antes solo
    se contaba el primero (tokens subcontados en positions)."""
    tot = 0.0
    for t in (tx.get("tokenTransfers") or []):
        if t.get("mint") != mint:
            continue
        if direction == "in" and t.get("toUserAccount") == wallet:
            tot += _tok_amount(t)
        elif direction == "out" and t.get("fromUserAccount") == wallet:
            tot += _tok_amount(t)
    return tot


def _fmt_amount(x) -> str:
    """Formatea cantidades de tokens: 1.20B, 850.00K, 1.50M…"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    a = abs(x)
    if a >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{x / 1_000:.2f}K"
    if a >= 1:
        return f"{x:.0f}"
    return f"{x:.4g}"


def _usd(x) -> str:
    """Importe en dólares, limpio y sin notación científica: $1,234 · $12.50."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "$?"
    a = abs(x)
    if a >= 1000:
        return f"${x:,.0f}"
    if a >= 1:
        return f"${x:,.2f}"
    return f"${x:.2f}"


def _sol_price() -> float | None:
    try:
        from unrealized_pnl import _sol_usd
        return _sol_usd()
    except Exception:
        return None


def _money(sol, su) -> str:
    """Importe en DÓLARES si conocemos el precio de SOL; si no, en SOL."""
    try:
        if su and su > 0:
            return _usd(float(sol) * su)
    except (TypeError, ValueError):
        pass
    try:
        return f"{float(sol):.2f} SOL"
    except (TypeError, ValueError):
        return "?"


def _money_signed(sol, su) -> str:
    try:
        v = float(sol)
    except (TypeError, ValueError):
        return "?"
    return ("+" if v >= 0 else "-") + _money(abs(v), su)


def _recarga_reciente(wallet: str, ts: int) -> float:
    """SOL recibido por transferencia directa en los ~30 min previos a la
    compra. Una recarga justo antes de comprar suele indicar convicción."""
    try:
        url = config.HELIUS_PARSED_TX.format(address=wallet)
        r = requests.get(url, params={"api-key": config.HELIUS_API_KEY,
                                      "limit": 50}, timeout=20)
        r.raise_for_status()
        total = 0.0
        for tx in r.json() or []:
            tts = tx.get("timestamp", 0)
            if not (ts - 1800 <= tts <= ts):
                continue
            for nt in tx.get("nativeTransfers") or []:
                if nt.get("toUserAccount") == wallet and \
                   nt.get("fromUserAccount") != wallet:
                    total += (nt.get("amount") or 0) / 1e9
        return total
    except Exception:
        return 0.0


# La lista de vigiladas se consultaba en CADA webhook, con dos conexiones
# nuevas cada vez. Cambia como mucho una vez por ciclo (cada 2 h), asi que
# 60 s de cache no pierden nada y quitan casi toda la presion sobre Postgres.
_VIG_TTL = 60.0
_VIG_LOCK = threading.Lock()
_VIG_CACHE = {"ts": 0.0, "watch": None, "stars": None}


def _listas_vigiladas(conn):
    """(vigiladas, ⭐, devs) reutilizando `conn` y con cache de 60 s."""
    ahora = time.time()
    with _VIG_LOCK:
        if (_VIG_CACHE["watch"] is not None
                and ahora - _VIG_CACHE["ts"] < _VIG_TTL):
            return (_VIG_CACHE["watch"], _VIG_CACHE["stars"],
                    _VIG_CACHE.get("devs") or set())
    w = watch_addresses(conn)
    e = tracked_addresses(conn)
    # (Ola 12b, afinado Ola 15 - H1) Devs como PARES (dev, mint): la
    # intercepcion aplica solo a la operacion del dev SOBRE SU TOKEN.
    # Antes se tragaba TODAS las operaciones de esa billetera — si el dev
    # era ademas candidata u huerfana, perdia su registro de señales y su
    # venta no cerraba el paper.
    try:
        d = {(r["dev_wallet"], r["mint"]) for r in conn.execute(
            "SELECT DISTINCT dev_wallet, mint FROM paper_trades "
            "WHERE status='abierta' AND dev_wallet IS NOT NULL").fetchall()}
    except Exception:
        d = set()
    with _VIG_LOCK:
        _VIG_CACHE.update({"ts": ahora, "watch": w, "stars": e, "devs": d})
    return w, e, d


def invalidar_vigiladas():
    """Fuerza releer la lista: se llama al cambiar las ⭐."""
    with _VIG_LOCK:
        _VIG_CACHE["ts"] = 0.0


def process_transactions(txs: list[dict]):
    # UNA conexion para todo el lote. Antes se abrian tres (watch_addresses,
    # tracked_addresses y esta) por cada webhook y por cada hilo, que es lo
    # que agotaba el cupo de Postgres.
    conn = get_conn()
    try:
        try:
            _proc(txs, conn)
        except Exception as e:
            # SQLite local (18/8): si otro trabajo retuvo la escritura mas
            # de la cuenta, reintentamos UNA vez en vez de perder el lote.
            # Es seguro re-procesar: el INSERT OR IGNORE por firma hace que
            # lo ya registrado se salte solo.
            if "locked" not in str(e).lower():
                # (Ola 18-E) El lote se pierde, pero al menos se ve. Antes
                # la excepcion subia y solo dejaba un `print` en el worker
                # de LaserStream: ni una linea en `/errores`, asi que una
                # señal perdida por un dato raro era invisible.
                try:
                    from errores import record as _rec_lote
                    _rec_lote("realtime.lote", e,
                              f"{len(txs)} transaccion(es) sin procesar")
                except Exception:
                    pass
                raise
            print("· Base ocupada; reintento el lote en 3 s")
            import time as _t
            _t.sleep(3)
            _proc(txs, conn)
    finally:
        conn.close()


def _proc(txs: list[dict], conn):
    lista_w, lista_e, devs = _listas_vigiladas(conn)
    tracked = set(lista_w)
    stars = set(lista_e)
    if not tracked:
        return
    # Solo alertan las mejores del ranking (mismo orden que /top). Las
    # demás ⭐ se siguen midiendo en silencio, igual que las candidatas.
    # Conjunto vacío = no se pudo calcular → no filtramos, para no dejar
    # el bot mudo por un fallo de consulta.
    top = top_addresses(conn)
    _devs_w = {w_ for w_, _m in devs}      # (Ola 16) fuera del bucle
    for tx in txs:
        trade = _detect_trade(tx, tracked)
        if not trade:
            continue
        # (Ola 12b, afinado Ola 15 - H1) Alarma de rug POR PAR: si el
        # dev vende SU token con posición abierta, alerta en segundos.
        # Sus operaciones en OTROS tokens solo se descartan si la
        # billetera no está vigilada por derecho propio (candidata,
        # ⭐ u huérfana del paper) — antes se tragaba todo su registro.
        if trade["wallet"] in _devs_w:
            if (trade["wallet"], trade["mint"]) in devs                     and trade["side"] == "venta":
                try:
                    from dev_watch import alerta_dev_inmediata
                    alerta_dev_inmediata(conn, trade)
                except Exception as e:
                    print(f"· dev_watch inmediato falló: {e}")
            if trade["wallet"] not in stars:
                _propia = conn.execute(
                    "SELECT 1 FROM wallets WHERE address=? AND "
                    "(is_tracked=1 OR (COALESCE(is_bot,0)=0 AND "
                    "winning_tokens_count>=2))",
                    (trade["wallet"],)).fetchone()
                if not _propia:
                    _propia = conn.execute(
                        "SELECT 1 FROM paper_trades WHERE status='abierta' "
                        "AND wallet=?", (trade["wallet"],)).fetchone()
                if not _propia:
                    continue      # dev puro: no contamina las señales
        # Candado anti-duplicados: solo un hilo puede "ganar" el registro
        # de esta firma; el resto la ve ya existente y no re-alerta.
        with _SIGNAL_LOCK:
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals (signature, wallet, mint, sol,"
                " ts, side) VALUES (?,?,?,?,?,?)",
                (trade["signature"], trade["wallet"], trade["mint"],
                 trade["sol"], trade["ts"], trade["side"]))
            conn.commit()
            es_nueva = cur.rowcount
        if not es_nueva:
            # RE-ENRIQUECIMIENTO (Ola 5, auditoria 19/8 - C14): si el
            # primer pase murio por "database is locked" DESPUES de
            # registrar la firma pero ANTES de guardar el precio, el
            # reintento veia la señal "ya procesada" y la saltaba:
            # price_usd quedaba NULL para siempre y la señal salia de
            # track_outcomes y de los hitos. Ahora se completa lo
            # medible (precio/simbolo/mc/liq); la alerta de ese caso
            # raro se da por perdida, los DATOS no.
            try:
                _f = conn.execute(
                    "SELECT price_usd FROM signals WHERE signature=?",
                    (trade["signature"],)).fetchone()
                if _f and _f["price_usd"] is None:
                    _t2 = analyze_token(trade["mint"])
                    if _t2.get("price"):
                        conn.execute(
                            "UPDATE signals SET price_usd=?, symbol=?, "
                            "mc=?, liq=? WHERE signature=?",
                            (_t2.get("price"), _t2.get("symbol"),
                             _t2.get("mc"), _t2.get("liq"),
                             trade["signature"]))
                        conn.commit()
                        print(f"· Señal {trade['signature'][:8]}… quedo a "
                              "medias; datos completados")
            except Exception as _e:
                print(f"· Re-enriquecimiento falló: {_e}")
            continue  # ya procesada, no re-alertar

        es_compra = trade["side"] == "compra"
        _px_caliente = None          # (Ola 17-H) ver mas abajo

        # Posición de la billetera en este token (acumulación / profit)
        # — bajo el candado del mint (M4): sin el, dos hilos del mismo
        # token pisaban el leer-modificar-escribir de positions.
        from db import apply_buy, apply_sell
        tokens_tx = trade.get("tokens") or 0.0
        with _lock_mint(trade["mint"]):
            if es_compra:
                pos = apply_buy(conn, trade["wallet"], trade["mint"],
                                trade["sol"], tokens_tx, trade["ts"])
            else:
                pos = apply_sell(conn, trade["wallet"], trade["mint"],
                                 trade["sol"], tokens_tx, trade["ts"])
        es_acum = bool(es_compra and pos.get("is_accumulation"))

        # ── CAMINO CALIENTE (copy trading rápido) ────────────────────
        # Si la operacion es de una ⭐ del top, el paper actua YA con una
        # sola consulta ligera de precio (~0,5 s). El analisis completo
        # (RugCheck, IA, alertas, motor predictivo) sigue corriendo
        # despues, para el registro y el chat, pero ya no retrasa la
        # copia: por la via normal pasaban 5-15 s entre la jugada de la
        # ⭐ y el paper; por aqui, 1-3 s. Es la antesala del executor
        # real, que colgara de este mismo punto.
        #   - Compra: abre el paper (idempotente: si la via normal llega
        #     despues, el candado "ya abierta" evita el duplicado).
        #   - Venta: cierra/holdea segun el perfil (tambien idempotente).
        # Copia TODA operacion de una ⭐ del top (asi funciona copiar
        # billeteras); el umbral de score solo gobierna las ALERTAS.
        # Apagable sin codigo: setting paper_rapido = 0.
        if trade["wallet"] in stars:
            try:
                if int(float(get_setting(conn, "paper_rapido", "1") or 1)):
                    en_top = (not top) or trade["wallet"] in top
                    _accion = None      # (tipo, trade_a_usar)
                    if es_compra and en_top:
                        _accion = ("abrir", trade, "top")
                    elif es_compra:
                        # ── COPIA POR CONSENSO (19/8, idea del dueño) ──
                        # N ⭐ de CUALQUIER liga comprando el mismo token
                        # en la ventana = señal de manada. Se copia aunque
                        # ninguna este en el top 30, imitando a la LIDER
                        # (la primera ⭐ en entrar): el trade queda a su
                        # nombre y su venta es la que se sigue. Etiqueta
                        # origen='consenso' para medirla aparte. Ajuste:
                        # consenso_copia_n (0 = apagado).
                        _n_min = int(float(get_setting(
                            conn, "consenso_copia_n", "3") or 3))
                        if _n_min > 0:
                            _since = trade["ts"] - CONSENSUS_WINDOW_MIN * 60
                            # Una sola consulta para conteo Y lider (el
                            # hilo del webhook no esta para escaneos
                            # dobles): compradores ⭐ ordenados por su
                            # primera entrada — el primero es la lider.
                            _mana = conn.execute(
                                "SELECT s.wallet, MIN(s.ts) t0 "
                                "FROM signals s JOIN wallets w "
                                "ON w.address=s.wallet AND w.is_tracked=1 "
                                "WHERE s.mint=? AND s.ts>=? "
                                "AND s.side='compra' "
                                "GROUP BY s.wallet ORDER BY t0 ASC",
                                (trade["mint"], _since)).fetchall()
                            if len(_mana) >= _n_min:
                                _t_lider = dict(trade)
                                _t_lider["wallet"] = _mana[0]["wallet"]
                                # (Ola 8) El monto del trade es del
                                # comprador que completo el quorum, no de
                                # la lider: mostrarlo atribuido a ella era
                                # un dato falso. Se busca SU compra; si no
                                # esta, mejor sin monto que con uno ajeno.
                                _sl = conn.execute(
                                    "SELECT sol FROM signals WHERE wallet=? "
                                    "AND mint=? AND side='compra' "
                                    "ORDER BY ts ASC LIMIT 1",
                                    (_mana[0]["wallet"],
                                     trade["mint"])).fetchone()
                                _t_lider["sol"] = (
                                    _sl["sol"] if _sl else None)
                                _accion = ("abrir", _t_lider, "consenso")
                    else:
                        # Venta: se sigue si es del top O si esta billetera
                        # es quien ABRIO una posicion viva (p. ej. la lider
                        # de un consenso fuera del top).
                        # Entra tambien si hay posicion de CONSENSO viva
                        # en este mint (la abrio la lider, pero cualquier
                        # ⭐ de la manada debe poder llegar al quorum de
                        # salida en paper_trading — hallazgo 19/8: sin
                        # este OR, el quorum era codigo muerto).
                        _sigue = en_top or conn.execute(
                            "SELECT 1 FROM paper_trades WHERE mint=? "
                            "AND status='abierta' AND (wallet=? "
                            "OR origen='consenso')",
                            (trade["mint"], trade["wallet"])).fetchone()
                        if _sigue:
                            _accion = ("cerrar", trade, None)
                    if _accion:
                        import paper_trading as _pt
                        from signal_tracker import _price_mc_ex as _pmx
                        # (Ola 16) La LIQUIDEZ viaja en el token: sin ella
                        # el suelo de open_trade (`liq < 1000`) se saltaba
                        # entero en el camino caliente — justo al revés de
                        # lo que decía su comentario.
                        _p0, _mc0, _muerto0, _liq0 = _pmx(trade["mint"])
                        # (Ola 17-H) Este precio es FRESCO y sin cache.
                        # Se guarda para no perderlo: veinte lineas mas
                        # abajo se escribia en `signals` el precio de
                        # `analyze_token`, que puede venir vacio, y la
                        # señal se quedaba sin medir para siempre
                        # teniendo el dato bueno en esta misma variable.
                        if _p0 and _p0 > 0:
                            _px_caliente = (_p0, _mc0, _liq0)
                        if _p0 and _p0 > 0:
                            _t0 = {"price": _p0, "symbol": trade["mint"][:6],
                                   "mc": _mc0, "liq": _liq0}
                            # Candado por mint (M4): el "una posicion por
                            # token" de open_trade es SELECT-then-INSERT.
                            with _lock_mint(trade["mint"]):
                                if _accion[0] == "abrir":
                                    _pt.open_trade(conn, _accion[1], _t0,
                                                   None, origen=_accion[2])
                                else:
                                    _pt.close_on_wallet_sell(conn, trade,
                                                             _t0, pos)
            except Exception as e:
                print(f"· Camino caliente falló ({e}); sigue la vía normal")

        since = trade["ts"] - CONSENSUS_WINDOW_MIN * 60
        consensus = conn.execute(
            "SELECT COUNT(DISTINCT s.wallet) c FROM signals s "
            "JOIN wallets w ON w.address = s.wallet AND w.is_tracked = 1 "
            "WHERE s.mint=? AND s.ts>=? AND s.side=?",
            (trade["mint"], since, trade["side"])).fetchone()["c"]

        t = analyze_token(trade["mint"])
        w = conn.execute(
            "SELECT ai_class, score, alias, pnl_30d, pnl_total, "
            "wallet_score, hold_median_min, roi_median "
            "FROM wallets WHERE address=?",
            (trade["wallet"],)).fetchone()

        # Guardar precio, símbolo, MC y liquidez del momento.
        # (Ola 17-H, auditoria 6) Medido en la base del dueño: el 34% de
        # las compras se quedaba SIN precio de entrada, y sin el la señal
        # nunca se mide (signal_tracker filtra `price_usd IS NOT NULL`).
        # Dos arreglos:
        #   a) si `analyze_token` no trajo precio pero el camino caliente
        #      SI lo consiguio, se usa ese en vez de escribir NULL;
        #   b) nunca se pisa con NULL un valor que ya estaba puesto.
        _px = t.get("price")
        _mcv, _liqv = t.get("mc"), t.get("liq")
        if not _px and _px_caliente:
            _px, _mcv, _liqv = (_px_caliente[0],
                                _mcv or _px_caliente[1],
                                _liqv or _px_caliente[2])
        if _px:
            conn.execute(
                "UPDATE signals SET price_usd=?, symbol=?, mc=?, liq=? "
                "WHERE signature=?",
                (_px, t.get("symbol"), _mcv, _liqv, trade["signature"]))
        else:
            # Sin precio: se guarda lo demas y se deja constancia para
            # que el re-enriquecimiento periodico lo reintente.
            conn.execute(
                "UPDATE signals SET symbol=COALESCE(?, symbol), "
                "mc=COALESCE(?, mc), liq=COALESCE(?, liq) "
                "WHERE signature=?",
                (t.get("symbol"), _mcv, _liqv, trade["signature"]))
        conn.commit()

        # ── Motor predictivo: decide si emitir una señal PREDICTIVA ──
        # (Ola 5) Esto estaba limitado a ⭐ por DOS motivos: que una
        # candidata no quedara como "lider" de prediccion, y que el grafo
        # de influencia (el caro) no se reconstruyera dentro del hilo de
        # ingesta por compras que no alimentan ninguna decision.
        # (Ola 18-G) El segundo motivo ya no existe: desde la 18-C el
        # camino caliente pide el grafo con `construir=False` y no
        # construye nada. Y el primero se resuelve mejor dentro de
        # `on_buy`, que ahora solo ABRE predicciones si es ⭐.
        #
        # El cambio importa porque las llegadas SOLO se registraban con
        # compras de ⭐: se predecian seguidores del grafo (25.000
        # billeteras) y se confirmaban contra las ⭐ (125). Medido: 93% de
        # las predicciones acababan con acierto 0 y NUNCA salio una
        # alerta en 20.785. Ahora confirma cualquier vigilada, que es
        # exactamente la poblacion que el bot puede ver.
        if es_compra and trade["wallet"] in tracked:
            try:
                from predictions import on_buy as _pred_on_buy
                # `on_buy` lee la prediccion abierta del token, le suma
                # este seguidor y la reescribe entera. Como cada peticion
                # corre en su propio hilo con su propia conexion, dos
                # seguidores del mismo token que llegan a la vez leian la
                # misma lista y el segundo pisaba al primero: una llegada
                # perdida, y con ella el acierto del lider a la baja. El
                # candado que lo evita se pide DENTRO de `on_buy`
                # (`_candado_mint`), y solo alrededor de esa escritura:
                # aqui abarcaria tambien el envio a Telegram, que hace red
                # y bloquearia la copia simulada de ese mismo token.
                # `tracked` y `stars` son conjuntos, no listas.
                _pred_on_buy(conn, trade["wallet"], trade["mint"],
                             trade["ts"], t, vigiladas=tracked,
                             es_estrella=trade["wallet"] in stars)
            except Exception as e:
                print(f"· Motor predictivo: {e}")

        # Track record real y patrón de MC de esta billetera
        try:
            from signal_tracker import wallet_track_record, format_track_record
            track = wallet_track_record(conn, trade["wallet"])
            track_line = format_track_record(track)
        except Exception:
            track, track_line = None, ""
        try:
            from wallet_score import wallet_pattern, format_pattern
            patron = wallet_pattern(conn, trade["wallet"])
            patron_line = format_pattern(patron, t.get("mc"))
        except Exception:
            patron, patron_line = None, ""

        # Score de la señal (0-100) y umbral configurable
        patron_ok = None
        if patron and t.get("mc"):
            patron_ok = (patron["mc_p25"] * 0.5 <= t["mc"]
                         <= patron["mc_p75"] * 2)
        # (Ola 10) Rol de liderazgo, SOLO del cache del grafo: en el hilo
        # del webhook jamas se construye (candado + RAM). Sin cache = s/d.
        try:
            from influence import influencia_ligera
            _rol = influencia_ligera(trade["wallet"])
        except Exception:
            _rol = None
        score_sig, desglose = compute_signal_score(
            t, _wget(w, "wallet_score"), track, consensus, patron_ok,
            inf=_rol)

        # Guardar el score (barato, sin IA). El veredicto de IA se calcula
        # MÁS ABAJO, solo si la señal supera TODOS los filtros — así no se
        # gastan créditos de IA en señales que se van a silenciar.
        verdict = {}
        conn.execute(
            "UPDATE signals SET signal_score=? WHERE signature=?",
            (score_sig, trade["signature"]))
        # ── Relleno de la posicion abierta por el camino caliente ──
        # El camino caliente abre el paper ANTES de conocer el ticker real
        # (pone el prefijo del mint) y antes de calcular el score (NULL).
        # La via normal ya no puede reabrir la posicion (candado "una por
        # token"), asi que RELLENA esos dos huecos en la fila existente.
        # Sin esto, /paper mostraba "7xKq4B" en vez de "BONK" y la columna
        # signal_score quedaba vacia para el analisis de scores.
        try:
            sym_real = t.get("symbol")
            if sym_real:
                conn.execute(
                    "UPDATE paper_trades SET symbol=? "
                    "WHERE signature=? AND symbol=?",
                    (sym_real, trade["signature"], trade["mint"][:6]))
            conn.execute(
                "UPDATE paper_trades SET signal_score=? "
                "WHERE signature=? AND signal_score IS NULL",
                (score_sig, trade["signature"]))
            conn.commit()
        except Exception as e:
            print(f"· Relleno de paper falló: {e}")
        conn.commit()

        # Liga de ascenso: candidatas sin ⭐ se miden en silencio.
        #
        # EXCEPCIÓN: una VENTA de una billetera que perdió la ⭐ pero aún
        # tiene una posición simulada ABIERTA sí se procesa, solo para
        # cerrarla. Si copiaste su compra querrías enterarte de su salida;
        # si no, esa posición acabaría cerrando por TP/SL/tiempo y la
        # simulación dejaría de medir lo que pretendía. No abre nada nuevo:
        # las compras de las no-⭐ se siguen ignorando.
        # Dos motivos para no alertar, con el mismo tratamiento: no tener
        # la ⭐, o tenerla pero estar fuera del top. En ambos casos la
        # operación se registra y se mide; solo no sale por el chat.
        es_star = trade["wallet"] in stars
        en_top = (not top) or trade["wallet"] in top
        if not es_star or not en_top:
            _cerrar_huerfana = False
            if not es_compra:
                try:
                    if es_star:
                        # Una ⭐ fuera del top tambien entra si hay una
                        # posicion de CONSENSO viva en el mint: el quorum
                        # de salida (paper_trading) decide si cierra.
                        # Sin este OR el quorum era codigo muerto (19/8).
                        _cerrar_huerfana = conn.execute(
                            "SELECT 1 FROM paper_trades WHERE mint=? "
                            "AND status='abierta' AND (wallet=? "
                            "OR origen='consenso') LIMIT 1",
                            (trade["mint"],
                             trade["wallet"])).fetchone() is not None
                    else:
                        _cerrar_huerfana = conn.execute(
                            "SELECT 1 FROM paper_trades WHERE wallet=? "
                            "AND mint=? AND status='abierta' LIMIT 1",
                            (trade["wallet"],
                             trade["mint"])).fetchone() is not None
                except Exception:
                    _cerrar_huerfana = False       # tabla aún sin crear
            if not _cerrar_huerfana:
                quien = "Candidata" if not es_star else "⭐ fuera del top"
                print(f"👁 {quien} {trade['wallet'][:8]}… {trade['side']} "
                      f"{trade['sol']:.2f} SOL — registrada sin alertar")
                continue
            motivo_h = ("ya no es ⭐" if not es_star
                        else "salió del top")
            print(f"🧪 Paper: {trade['wallet'][:8]}… {motivo_h} pero vendió "
                  f"{t['symbol']}; evalúo el cierre de la simulada")
            try:
                import paper_trading
                paper_trading.close_on_wallet_sell(
                    conn, trade, t, pos, sigue_estrella=es_star)
            except Exception as e:
                print(f"· Cierre de paper huérfana falló: {e}")
            continue

        # Filtro: señales (COMPRA y VENTA) bajo el umbral no alertan (sí se miden)
        umbral = float(get_setting(conn, "min_signal_score", "0") or 0)
        if score_sig < umbral:
            print(f"🔇 Señal {t['symbol']} ({trade['side']}) silenciada: "
                  f"score {score_sig} < umbral {umbral:.0f}")
            # La VENTA silenciada igual cierra el paper (Ola 6 - M2): el
            # silencio es de la ALERTA, no de la copia. El camino
            # caliente ya lo intento, pero si su consulta de precio fallo
            # (DexScreener transitorio) la posicion quedaba viva hasta
            # TP/SL/tiempo, corrompiendo lo que la simulacion mide. Es
            # idempotente via paper_fills: si ya se proceso, no-op.
            if not es_compra:
                try:
                    import paper_trading
                    paper_trading.close_on_wallet_sell(conn, trade, t, pos)
                except Exception as e:
                    print(f"· Paper (venta silenciada) falló: {e}")
            continue

        # Máximo por señal: cap de alertas por billetera y por token en
        # la última hora (configurable en settings). Las señales tapadas
        # se siguen midiendo (track record), solo no alertan.
        # Topes anti-spam: por billetera, por token y GLOBAL por hora.
        # Se aplican a COMPRAS y VENTAS (antes solo a compras → las ventas
        # inundaban el chat). Todo configurable en settings.
        try:
            max_w = int(float(get_setting(conn, "max_alertas_wallet", "3") or 3))
            max_t = int(float(get_setting(conn, "max_alertas_token", "2") or 2))
            max_h = int(float(get_setting(conn, "max_alertas_hora", "12") or 12))
        except (TypeError, ValueError):
            max_w, max_t, max_h = 3, 2, 12
        hace_1h = trade["ts"] - 3600
        _side = trade["side"]
        # (Ola 17-M) Los topes cuentan `alert_intento`, no `alerted`.
        # Desde la 17-L `alerted` solo se pone si Telegram acepto, y con
        # eso un 429 dejaba de gastar cupo: la tormenta de reintentos se
        # quedaba sin cortacircuito justo cuando hacia falta. El intento
        # gasta cupo aunque el mensaje no llegue; `alerted` sigue
        # significando "llego". Las filas viejas no tienen la columna
        # puesta, asi que se cuenta tambien `alerted=1` para no perder de
        # golpe la memoria del tope en el primer arranque tras el deploy.
        # (El SQL va literal, sin concatenar: `auditoria.py` solo revisa
        # las consultas que son una cadena constante.)
        n_w = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE wallet=? AND ts>=? "
            "AND side=? AND (COALESCE(alert_intento, 0)=1 OR alerted=1) "
            "AND signature<>?",
            (trade["wallet"], hace_1h, _side, trade["signature"])).fetchone()["c"]
        n_t = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE mint=? AND ts>=? "
            "AND side=? AND (COALESCE(alert_intento, 0)=1 OR alerted=1) "
            "AND signature<>?",
            (trade["mint"], hace_1h, _side, trade["signature"])).fetchone()["c"]
        n_h = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE ts>=? "
            "AND (COALESCE(alert_intento, 0)=1 OR alerted=1) "
            "AND signature<>?",
            (hace_1h, trade["signature"])).fetchone()["c"]
        if n_w >= max_w or n_t >= max_t or n_h >= max_h:
            if n_h >= max_h:
                motivo = f"global ({n_h}/{max_h} en 1h)"
            elif n_w >= max_w:
                motivo = f"billetera ({n_w}/{max_w} en 1h)"
            else:
                motivo = f"token ({n_t}/{max_t} en 1h)"
            print(f"🔇 Señal {t['symbol']} ({_side}) silenciada: "
                  f"máximo por {motivo} alcanzado")
            if not es_compra:      # mismo criterio que el umbral (M2)
                try:
                    import paper_trading
                    paper_trading.close_on_wallet_sell(conn, trade, t, pos)
                except Exception as e:
                    print(f"· Paper (venta capada) falló: {e}")
            continue

        # ── Veredicto de IA: SOLO para señales que SÍ se alertan ──
        # (pasaron ⭐ + umbral + topes). Aquí es donde se gasta la IA, ya
        # filtrado, para no quemar créditos en el ruido.
        aprendizajes = get_setting(conn, "learnings", None)
        importante = consensus >= 2 or trade["sol"] >= 5 or score_sig >= 75
        try:
            from ai_budget import can_call
            _hay_ia = can_call(conn)
        except Exception:
            _hay_ia = True
        if _hay_ia:
            verdict = _ai_signal_verdict(conn=conn, payload={
                "accion": trade["side"],
                "token": ai_payload(t),
                "monto_sol": round(trade["sol"], 2),
                "billetera_clase": _wget(w, "ai_class"),
                "track_record_billetera": track,
                "patron_mc_billetera": patron,
                "consenso_billeteras": consensus,
                "score_senal": score_sig,
                "aprendizajes_del_sistema": (aprendizajes or "")[:600] or None,
            }, smart=importante) or {}
        else:
            verdict = {}
        conn.execute("UPDATE signals SET verdict=? WHERE signature=?",
                     (verdict.get("veredicto"), trade["signature"]))
        conn.commit()

        # Convicción: ¿recibió SOL fresco justo antes de comprar?
        if es_compra:
            recarga = _recarga_reciente(trade["wallet"], trade["ts"])
            if recarga >= 1:
                tg_send(f"⚡ *Convicción alta:* la billetera recargó "
                        f"{recarga:.1f} SOL minutos antes de esta compra.")

        if es_acum:
            side_icon = "🟢➕"
            side_txt = f"ACUMULANDO · compra #{pos['buys']}"
            verbo = "Acumuló"
        elif es_compra:
            side_icon = "🟢"
            side_txt = "COMPRA"
            verbo = "Compró"
        else:
            side_icon = "🔴"
            side_txt = "VENTA"
            verbo = "Vendió"
        v_icon = {"entrar": "🟢", "precaucion": "🟡", "evitar": "🔴",
                  "salir": "🚪"}.get(verdict.get("veredicto"), "⚪")
        cons_txt = (f"\n🔥 *CONSENSO: {consensus} billeteras ⭐ "
                    f"{'compraron' if es_compra else 'vendieron'} este token "
                    f"en {CONSENSUS_WINDOW_MIN} min*"
                    if consensus >= 2 else "")
        token_block = format_token_block(t)
        links = []
        if t["websites"]:
            links.append("🌐 " + " · ".join(t["websites"]))
        if t["socials"]:
            links.append("📱 " + "\n📱 ".join(t["socials"]))
        redes = ("\n" + "\n".join(links) + "\n") if links else ""

        # Nombre SIEMPRE legible + posición en /top (mismo criterio que la
        # tarjeta x2: la dirección cruda no le dice nada a nadie).
        try:
            from wallet_ident import identidad
            _id = identidad(conn, trade["wallet"])
            alias, _postop = _id["nombre"], _id.get("pos")
        except Exception:
            alias, _postop = (_wget(w, "alias")
                              or f"{trade['wallet'][:8]}…"), None
        clase = _wget(w, "ai_class") or "?"
        su = _sol_price()   # SOL/USD para mostrar importes en dólares
        pnl30, pnltot = _wget(w, "pnl_30d"), _wget(w, "pnl_total")
        partes = []
        if pnl30 is not None:
            partes.append(f"30d: {_money_signed(pnl30, su)}")
        if pnltot is not None:
            partes.append(f"histórico: {_money_signed(pnltot, su)}")
        # (Ola 8) El PnL esta guardado en SOL; el $ mostrado usa el cambio
        # de HOY sobre ganancias historicas — cifra que nunca existio tal
        # cual. Se muestra el SOL real y el $ queda como aproximacion.
        pnl_txt = (("\n💰 PnL billetera → " + " · ".join(partes)
                    + f"\n_(SOL: 30d {pnl30:+.1f} · hist {pnltot:+.1f}"
                      f" · $ ≈ al cambio actual)_")
                   if partes and pnl30 is not None and pnltot is not None
                   else ("\n💰 PnL billetera → " + " · ".join(partes)
                         + " _( ≈ al cambio actual)_") if partes else "")
        track_txt = f"\n{track_line}" if track_line else ""
        pat_txt = f"\n{patron_line}" if patron_line else ""

        # Bloque de posición: tokens obtenidos/vendidos, total y profit
        sym = t.get('symbol') or trade['mint'][:6]
        if es_compra:
            linea_sol = (f"💵 {verbo}: *{_money(trade['sol'], su)}*"
                         f"  ·  {trade['sol']:.2f} SOL")
            pos_txt = (
                f"\n📥 Obtuvo: *{_fmt_amount(pos['tokens_bought'])} {sym}*"
                f"\n📦 Tiene ahora: *{_fmt_amount(pos['total_tokens'])} {sym}*"
                f"  ·  invertido {_money(pos['sol_invertido'], su)}")
        else:
            linea_sol = (f"💵 Recibió: *{_money(trade['sol'], su)}*"
                         f"  ·  {trade['sol']:.2f} SOL")
            if pos.get("known"):
                pl = pos["realized_this"] or 0.0
                pl_icon = "🟢" if pl >= 0 else "🔴"
                if pos["fully_sold"]:
                    resto = "✅ Vendió el *100%* (cerró la posición)"
                else:
                    resto = (f"📦 Le queda: *{_fmt_amount(pos['remaining_tokens'])} {sym}*"
                             f"  ·  vendió *{pos['pct_sold']:.0f}%*")
                # (Ola 8) Si vendio mas de lo que el bot le vio comprar,
                # el PnL solo cubre la parte rastreada — y se dice.
                extra = pos.get("tokens_no_rastreados") or 0.0
                nota = (f"\n_(vendió además {_fmt_amount(extra)} {sym} "
                        f"comprados antes del rastreo; su PnL no se cuenta)_"
                        if extra > 0 else "")
                pos_txt = (
                    f"\n📤 Vendió: *{_fmt_amount(pos['tokens_sold'])} {sym}*"
                    f"\n{pl_icon} Profit realizado: *{_money_signed(pl, su)}*"
                    f"  (total {_money_signed(pos['realized_total'], su)})"
                    f"\n{resto}{nota}")
            else:
                pos_txt = (
                    f"\n📤 Vendió: *{_fmt_amount(pos['tokens_sold'])} {sym}*"
                    f"\n_(no vi su compra; profit desconocido)_")

        bar = _bar(score_sig)
        salida_txt = _plan_salida(w) if es_compra else ""
        div = "━━━━━━━━━━━━━━"
        _entregada = tg_send(
            f"{side_icon} *{side_txt}* de billetera ⭐{cons_txt}\n"
            f"{div}\n"
            f"💎 *{t['symbol']}*\n`{trade['mint']}`\n\n"
            f"🎯 Señal  {bar}  *{score_sig}/100*\n\n"
            f"👤 *{alias}*"
            + (f"  ·  🏆 #{_postop} del top" if _postop else "")
            + f"  ·  _{clase}_\n"
            f"{linea_sol}{pos_txt}"
            f"{pnl_txt}{track_txt}{pat_txt}{salida_txt}\n"
            f"{div}\n"
            f"📋 *Token*\n{token_block}{redes}\n"
            f"{div}\n"
            f"{v_icon} *{(verdict.get('veredicto') or 'sin veredicto').upper()}*\n"
            f"_{verdict.get('razon') or ''}_\n\n"
            f"📊 [DexScreener](https://dexscreener.com/solana/{trade['mint']})"
            f"  ·  📈 [GMGN](https://gmgn.ai/sol/token/{trade['mint']})",
            buttons=[[("📋 Ficha", f"ficha:{trade['wallet']}"),
                      ("💰 Saldo", f"saldo1:{trade['wallet']}"),
                      ("❌ Descartar", f"adel:{trade['wallet']}")]])
        print(f"📡 Señal {trade['side']}: {t['symbol']} "
              f"por {trade['wallet'][:8]}")

        # Marca la señal como alertada (para el cap de máximos por señal).
        # (Ola 17-L) SOLO si Telegram la aceptó. `alerted` es la marca
        # historica que usan los topes anti-spam y el chequeo de Medicion
        # de /salud; ponerla tras un envio rechazado hacia dos daños a la
        # vez: la alerta perdida gastaba cupo del tope (tapando la
        # siguiente, que quizas si hubiera entrado) y la base afirmaba un
        # envio que no ocurrio. El paper SI se abre igual: la simulacion
        # mide la señal, no si el mensaje llego.
        # (Ola 17-M) El INTENTO se marca siempre: es lo que gasta cupo en
        # los topes anti-spam de arriba. `alerted` solo si Telegram la
        # acepto.
        conn.execute("UPDATE signals SET alert_intento=1 WHERE signature=?",
                     (trade["signature"],))
        if _entregada:
            conn.execute("UPDATE signals SET alerted=1 WHERE signature=?",
                         (trade["signature"],))
        else:
            print(f"· La alerta de {t['symbol']} NO se pudo entregar; "
                  f"la señal NO se marca como alertada (queda en /errores)")
        conn.commit()

        # Paper trading: abre posición simulada con la compra alertada;
        # si es venta de la ⭐ que dio la señal, cierra la simulada.
        try:
            import paper_trading
            if es_compra:
                paper_trading.open_trade(conn, trade, t, score_sig)
            else:
                paper_trading.close_on_wallet_sell(conn, trade, t, pos)
        except Exception as e:
            print(f"· Paper trading falló: {e}")


flask_app = Flask(__name__)

# Mini App de Telegram (panel visual en /app)
try:
    from webapp import register_webapp
    register_webapp(flask_app)
except Exception as e:
    print(f"· Mini app no disponible: {e}")


@flask_app.get("/")
def health():
    return jsonify({"ok": True, "tracked": len(tracked_addresses())})


@flask_app.post("/helius")
def helius_hook():
    global LAST_HOOK_TS
    LAST_HOOK_TS = time.time()
    auth = request.headers.get("Authorization", "")
    if auth != config.HELIUS_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    txs = request.get_json(silent=True) or []
    if isinstance(txs, dict):
        txs = [txs]
    threading.Thread(target=process_transactions, args=(txs,),
                     daemon=True).start()
    return jsonify({"ok": True})


def start_webhook_server():
    """Arranca Flask en un hilo demonio (no bloquea al bot)."""
    t = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT,
                                     debug=False, use_reloader=False),
        daemon=True)
    t.start()
    print(f"📡 Servidor de webhooks escuchando en puerto {PORT}")
