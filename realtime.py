"""
Fase 2 — Monitoreo en tiempo real de billeteras ⭐.

Piezas:
  1. Servidor de webhooks (Flask): recibe de Helius cada transacción
     de las billeteras rastreadas, al instante.
  2. Sincronizador: registra/actualiza el webhook en Helius con la
     lista actual de billeteras ⭐ (se llama al final de cada ciclo).
  3. Motor de señales: cuando una ⭐ COMPRA un token →
     RugCheck (seguridad) + DexScreener (datos) + consenso entre
     billeteras + veredicto IA → alerta a Telegram.

Variables de entorno usadas:
  PUBLIC_URL   → dominio público de Railway (ej. worker-xxx.up.railway.app)
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
from db import get_conn

LAMPORTS = 1_000_000_000
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

HELIUS_WEBHOOKS = "https://api.helius.xyz/v0/webhooks"
RUGCHECK_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",   # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
MIN_SIGNAL_SOL = 0.3          # ignorar compras menores
CONSENSUS_WINDOW_MIN = 45     # ventana para detectar consenso


# ══════════════ utilidades ══════════════

def tg_send(text: str):
    """Envía mensaje al admin vía HTTP API (seguro desde cualquier hilo)."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": int(ADMIN_ID), "text": text,
                  "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=15)
    except requests.RequestException as e:
        print(f"· No se pudo enviar alerta TG: {e}")


def tracked_addresses() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT address FROM wallets WHERE is_tracked=1").fetchall()
    conn.close()
    return [r["address"] for r in rows]


# ══════════════ sincronización del webhook en Helius ══════════════

def sync_helius_webhook() -> str:
    """Crea o actualiza el webhook de Helius con las billeteras ⭐."""
    if not PUBLIC_URL:
        return "PUBLIC_URL no configurada; webhook no sincronizado"
    addrs = tracked_addresses()
    if not addrs:
        return "Sin billeteras ⭐ aún; nada que monitorear"

    hook_url = f"https://{PUBLIC_URL}/helius"
    params = {"api-key": config.HELIUS_API_KEY}
    body = {
        "webhookURL": hook_url,
        "transactionTypes": ["ANY"],
        "accountAddresses": addrs,
        "webhookType": "enhanced",
        "authHeader": config.HELIUS_API_KEY,  # para validar origen
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


# ══════════════ motor de señales ══════════════

def _rugcheck(mint: str) -> dict:
    try:
        r = requests.get(RUGCHECK_SUMMARY.format(mint=mint), timeout=15)
        if r.status_code == 200:
            d = r.json()
            risks = [x.get("name", "") for x in (d.get("risks") or [])
                     if x.get("level") in ("danger", "warn")]
            return {"score": d.get("score"), "risks": risks[:5]}
    except requests.RequestException:
        pass
    return {"score": None, "risks": []}


def _token_info(mint: str) -> dict:
    try:
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=mint),
                         timeout=15)
        pairs = (r.json() or {}).get("pairs") or []
        if pairs:
            p = pairs[0]
            return {
                "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
                "liq": (p.get("liquidity") or {}).get("usd"),
                "mc": p.get("fdv"),
                "price_change_h1": (p.get("priceChange") or {}).get("h1"),
            }
    except requests.RequestException:
        pass
    return {"symbol": "?", "liq": None, "mc": None, "price_change_h1": None}


def _ai_signal_verdict(payload: dict) -> dict | None:
    if not ANTHROPIC_API_KEY:
        return None
    prompt = (
        "Eres analista de riesgo en memecoins de Solana. Una billetera "
        "rastreada (clasificada como trader/sniper rentable) acaba de "
        "comprar este token. Datos:\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
        'Responde SOLO JSON: {"veredicto":"entrar"|"precaucion"|"evitar",'
        '"razon":"máx 2 frases en español"}')
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception as e:
        print(f"· IA señal falló: {e}")
        return None


def _detect_buy(tx: dict, tracked: set[str]) -> dict | None:
    """¿Alguna billetera rastreada compró un token en esta tx?"""
    if tx.get("transactionError"):
        return None
    for t in (tx.get("tokenTransfers") or []):
        mint = t.get("mint")
        buyer = t.get("toUserAccount")
        if not mint or mint in IGNORED_MINTS or buyer not in tracked:
            continue
        sol_out = 0.0
        for acc in (tx.get("accountData") or []):
            if acc.get("account") == buyer:
                delta = int(acc.get("nativeBalanceChange", 0))
                if delta < 0:
                    sol_out = abs(delta) / LAMPORTS
                break
        if sol_out >= MIN_SIGNAL_SOL:
            return {"wallet": buyer, "mint": mint, "sol": sol_out,
                    "signature": tx.get("signature", ""),
                    "ts": tx.get("timestamp") or int(time.time())}
    return None


def process_transactions(txs: list[dict]):
    tracked = set(tracked_addresses())
    if not tracked:
        return
    conn = get_conn()
    for tx in txs:
        buy = _detect_buy(tx, tracked)
        if not buy:
            continue
        # dedupe por firma
        cur = conn.execute(
            "INSERT OR IGNORE INTO signals (signature, wallet, mint, sol, ts)"
            " VALUES (?,?,?,?,?)",
            (buy["signature"], buy["wallet"], buy["mint"], buy["sol"],
             buy["ts"]))
        conn.commit()
        if not cur.rowcount:
            continue  # ya procesada

        # consenso: ¿cuántas ⭐ distintas compraron este mint hace poco?
        since = buy["ts"] - CONSENSUS_WINDOW_MIN * 60
        consensus = conn.execute(
            "SELECT COUNT(DISTINCT wallet) c FROM signals "
            "WHERE mint=? AND ts>=?", (buy["mint"], since)).fetchone()["c"]

        info = _token_info(buy["mint"])
        rug = _rugcheck(buy["mint"])
        w = conn.execute(
            "SELECT ai_class, score FROM wallets WHERE address=?",
            (buy["wallet"],)).fetchone()

        verdict = _ai_signal_verdict({
            "token": info, "rugcheck": rug,
            "compra_sol": round(buy["sol"], 2),
            "billetera_clase": w["ai_class"] if w else None,
            "consenso_billeteras": consensus,
        }) or {}

        icon = {"entrar": "🟢", "precaucion": "🟡",
                "evitar": "🔴"}.get(verdict.get("veredicto"), "⚪")
        cons_txt = (f"\n🔥 *CONSENSO: {consensus} billeteras ⭐ "
                    f"compraron este token en {CONSENSUS_WINDOW_MIN} min*"
                    if consensus >= 2 else "")
        liq = f"${info['liq']:,.0f}" if info.get("liq") else "?"
        mc = f"${info['mc']:,.0f}" if info.get("mc") else "?"
        risks = ("\n⚠️ Riesgos: " + ", ".join(rug["risks"])) if rug["risks"] else ""

        tg_send(
            f"{icon} *SEÑAL: compra de billetera ⭐*{cons_txt}\n\n"
            f"Token: *{info['symbol']}*\n`{buy['mint']}`\n"
            f"Liquidez: {liq} · MC: {mc}\n"
            f"Compró: {buy['sol']:.2f} SOL · billetera "
            f"`{buy['wallet'][:8]}…` ({w['ai_class'] if w else '?'})"
            f"{risks}\n\n"
            f"🧠 *{verdict.get('veredicto', 'sin veredicto').upper()}*: "
            f"_{verdict.get('razon', '')}_\n\n"
            f"📊 gmgn.ai/sol/token/{buy['mint']}")
        print(f"📡 Señal enviada: {info['symbol']} por {buy['wallet'][:8]}")
    conn.close()


# ══════════════ servidor Flask ══════════════

flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return jsonify({"ok": True, "tracked": len(tracked_addresses())})


@flask_app.post("/helius")
def helius_hook():
    # Validar que viene de nuestro webhook (authHeader configurado)
    auth = request.headers.get("Authorization", "")
    if auth != config.HELIUS_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    txs = request.get_json(silent=True) or []
    if isinstance(txs, dict):
        txs = [txs]
    # Procesar en segundo plano para responder rápido a Helius
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
              
