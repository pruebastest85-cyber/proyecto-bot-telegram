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
from db import get_conn, get_setting
from token_check import analyze_token, format_token_block, ai_payload
from signal_score import compute_signal_score

LAMPORTS = 1_000_000_000
LAST_HOOK_TS = None   # última vez que Helius nos mandó algo (watchdog)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Candado anti-duplicados: serializa el "quién registra primero la señal"
# para que dos hilos (entregas simultáneas del mismo webhook) no alerten
# la misma operación dos veces.
_SIGNAL_LOCK = threading.Lock()

HELIUS_WEBHOOKS = "https://api.helius.xyz/v0/webhooks"

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
MIN_SIGNAL_SOL = 0.3
CONSENSUS_WINDOW_MIN = 45


def tg_send(text: str, buttons: list | None = None):
    """Envía mensaje al admin vía HTTP API (seguro desde cualquier hilo).
    buttons: lista de filas [[(texto, callback_data), …], …]."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
    payload = {"chat_id": int(ADMIN_ID), "text": text,
               "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": tx, "callback_data": cb} for tx, cb in fila]
            for fila in buttons]}
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15)
    except requests.RequestException as e:
        print(f"· No se pudo enviar alerta TG: {e}")


def tg_send_photo(photo_bytes: bytes, caption: str = ""):
    """Envía una foto con caption (Markdown) al admin — para tarjetas."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": int(ADMIN_ID), "caption": caption[:1000],
                  "parse_mode": "Markdown"},
            files={"photo": ("card.jpg", photo_bytes, "image/jpeg")},
            timeout=25)
    except requests.RequestException as e:
        print(f"· No se pudo enviar foto TG: {e}")


def tracked_addresses() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT address FROM wallets WHERE is_tracked=1").fetchall()
    conn.close()
    return [r["address"] for r in rows]


def watch_addresses() -> list[str]:
    """⭐ + candidatas prometedoras (liga de ascenso): las candidatas se
    monitorean en silencio para construir su track record real antes de
    darles la estrella."""
    conn = get_conn()
    stars = [r["address"] for r in conn.execute(
        "SELECT address FROM wallets WHERE is_tracked=1").fetchall()]
    cands = [r["address"] for r in conn.execute(
        """SELECT address FROM wallets
           WHERE is_tracked=0 AND is_bot=0 AND winning_tokens_count >= 2
           ORDER BY score DESC LIMIT 40""").fetchall()]
    conn.close()
    return stars + [c for c in cands if c not in stars]


def sync_helius_webhook() -> str:
    """Crea o actualiza el webhook de Helius con las billeteras ⭐."""
    if not PUBLIC_URL:
        return "PUBLIC_URL no configurada; webhook no sincronizado"
    addrs = watch_addresses()
    if not addrs:
        return "Sin billeteras ⭐ aún; nada que monitorear"

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
            return f"Webhook actualizado: {len(addrs)} billeteras vigiladas"
        r = requests.post(HELIUS_WEBHOOKS, params=params, json=body,
                          timeout=20)
        r.raise_for_status()
        return f"Webhook creado: {len(addrs)} billeteras vigiladas"
    except requests.RequestException as e:
        return f"Error sincronizando webhook: {e}"


MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_SMART = os.getenv("AI_SMART_MODEL", "claude-sonnet-5")


def _ai_signal_verdict(payload: dict, smart: bool = False) -> dict | None:
    """Veredicto IA de la señal. smart=True usa el modelo potente
    (señales importantes: consenso o montos grandes)."""
    if not ANTHROPIC_API_KEY:
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
    modelo = MODEL_SMART if smart else MODEL_FAST
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": modelo, "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception as e:
        print(f"· IA señal falló ({modelo}): {e}")
        if smart:  # respaldo: reintentar con el modelo rápido
            return _ai_signal_verdict(payload, smart=False)
        return None


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
                        "side": "compra", "tokens": _tok_amount(t),
                        "signature": tx.get("signature", ""),
                        "ts": tx.get("timestamp") or int(time.time())}

        seller = t.get("fromUserAccount")
        if seller in tracked:
            delta = _wallet_sol_delta(tx, seller)
            if delta > 0 and delta >= MIN_SIGNAL_SOL:
                return {"wallet": seller, "mint": mint, "sol": delta,
                        "side": "venta", "tokens": _tok_amount(t),
                        "signature": tx.get("signature", ""),
                        "ts": tx.get("timestamp") or int(time.time())}
    return None


def _wget(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


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


def process_transactions(txs: list[dict]):
    tracked = set(watch_addresses())
    stars = set(tracked_addresses())
    if not tracked:
        return
    conn = get_conn()
    for tx in txs:
        trade = _detect_trade(tx, tracked)
        if not trade:
            continue
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
            continue  # ya procesada, no re-alertar

        es_compra = trade["side"] == "compra"

        # Posición de la billetera en este token (acumulación / profit)
        from db import apply_buy, apply_sell
        tokens_tx = trade.get("tokens") or 0.0
        if es_compra:
            pos = apply_buy(conn, trade["wallet"], trade["mint"],
                            trade["sol"], tokens_tx, trade["ts"])
        else:
            pos = apply_sell(conn, trade["wallet"], trade["mint"],
                             trade["sol"], tokens_tx, trade["ts"])
        es_acum = bool(es_compra and pos.get("is_accumulation"))

        since = trade["ts"] - CONSENSUS_WINDOW_MIN * 60
        consensus = conn.execute(
            "SELECT COUNT(DISTINCT wallet) c FROM signals "
            "WHERE mint=? AND ts>=? AND side=?",
            (trade["mint"], since, trade["side"])).fetchone()["c"]

        t = analyze_token(trade["mint"])
        w = conn.execute(
            "SELECT ai_class, score, alias, pnl_30d, pnl_total "
            "FROM wallets WHERE address=?",
            (trade["wallet"],)).fetchone()

        # Guardar precio, símbolo, MC y liquidez del momento
        conn.execute(
            "UPDATE signals SET price_usd=?, symbol=?, mc=?, liq=? "
            "WHERE signature=?",
            (t.get("price"), t.get("symbol"), t.get("mc"), t.get("liq"),
             trade["signature"]))
        conn.commit()

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
        score_sig, desglose = compute_signal_score(
            t, _wget(w, "wallet_score"), track, consensus, patron_ok)

        # Aprendizajes del informe semanal (si existen)
        aprendizajes = get_setting(conn, "learnings", None)

        # Señal importante → modelo potente
        importante = consensus >= 2 or trade["sol"] >= 5 or score_sig >= 75
        verdict = _ai_signal_verdict({
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

        # Guardar score y veredicto para el aprendizaje futuro
        conn.execute(
            "UPDATE signals SET signal_score=?, verdict=? WHERE signature=?",
            (score_sig, verdict.get("veredicto"), trade["signature"]))
        conn.commit()

        # Liga de ascenso: candidatas sin ⭐ se miden en silencio
        if trade["wallet"] not in stars:
            print(f"👁 Candidata {trade['wallet'][:8]}… {trade['side']} "
                  f"{trade['sol']:.2f} SOL — registrada sin alertar")
            continue

        # Filtro: señales de compra bajo el umbral no alertan (sí se miden)
        umbral = float(get_setting(conn, "min_signal_score", "0") or 0)
        if es_compra and score_sig < umbral:
            print(f"🔇 Señal {t['symbol']} silenciada: "
                  f"score {score_sig} < umbral {umbral:.0f}")
            continue

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

        alias = _wget(w, "alias") or f"{trade['wallet'][:8]}…"
        clase = _wget(w, "ai_class") or "?"
        pnl30, pnltot = _wget(w, "pnl_30d"), _wget(w, "pnl_total")
        partes = []
        if pnl30 is not None:
            partes.append(f"30d: {pnl30:+.1f} SOL")
        if pnltot is not None:
            partes.append(f"histórico: {pnltot:+.1f} SOL")
        pnl_txt = ("\n💰 PnL billetera → " + " · ".join(partes)) if partes else ""
        track_txt = f"\n{track_line}" if track_line else ""
        pat_txt = f"\n{patron_line}" if patron_line else ""

        # Bloque de posición: tokens obtenidos/vendidos, total y profit
        sym = t.get('symbol') or trade['mint'][:6]
        if es_compra:
            linea_sol = f"💵 {verbo}: *{trade['sol']:.2f} SOL*"
            pos_txt = (
                f"\n📥 Obtuvo: *{_fmt_amount(pos['tokens_bought'])} {sym}*"
                f"\n📦 Tiene ahora: *{_fmt_amount(pos['total_tokens'])} {sym}*"
                f"  ·  invertido {pos['sol_invertido']:.2f} SOL")
        else:
            linea_sol = f"💵 Recibió: *{trade['sol']:.2f} SOL*"
            if pos.get("known"):
                pl = pos["realized_this"] or 0.0
                pl_icon = "🟢" if pl >= 0 else "🔴"
                if pos["fully_sold"]:
                    resto = "✅ Vendió el *100%* (cerró la posición)"
                else:
                    resto = (f"📦 Le queda: *{_fmt_amount(pos['remaining_tokens'])} {sym}*"
                             f"  ·  vendió *{pos['pct_sold']:.0f}%*")
                pos_txt = (
                    f"\n📤 Vendió: *{_fmt_amount(pos['tokens_sold'])} {sym}*"
                    f"\n{pl_icon} Profit realizado: *{pl:+.2f} SOL*"
                    f"  (total {pos['realized_total']:+.2f} SOL)"
                    f"\n{resto}")
            else:
                pos_txt = (
                    f"\n📤 Vendió: *{_fmt_amount(pos['tokens_sold'])} {sym}*"
                    f"\n_(no vi su compra; profit desconocido)_")

        bar = _bar(score_sig)
        div = "━━━━━━━━━━━━━━"
        tg_send(
            f"{side_icon} *{side_txt}* de billetera ⭐{cons_txt}\n"
            f"{div}\n"
            f"💎 *{t['symbol']}*\n`{trade['mint']}`\n\n"
            f"🎯 Señal  {bar}  *{score_sig}/100*\n\n"
            f"👤 *{alias}*  ·  _{clase}_\n"
            f"{linea_sol}{pos_txt}"
            f"{pnl_txt}{track_txt}{pat_txt}\n"
            f"{div}\n"
            f"📋 *Token*\n{token_block}{redes}\n"
            f"{div}\n"
            f"{v_icon} *{verdict.get('veredicto', 'sin veredicto').upper()}*\n"
            f"_{verdict.get('razon', '')}_\n\n"
            f"📊 [DexScreener](https://dexscreener.com/solana/{trade['mint']})"
            f"  ·  📈 [GMGN](https://gmgn.ai/sol/token/{trade['mint']})",
            buttons=[[("📋 Ficha", f"ficha:{trade['wallet']}"),
                      ("💰 Saldo", f"saldo1:{trade['wallet']}"),
                      ("❌ Descartar", f"adel:{trade['wallet']}")]])
        print(f"📡 Señal {trade['side']}: {t['symbol']} "
              f"por {trade['wallet'][:8]}")
    conn.close()


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
