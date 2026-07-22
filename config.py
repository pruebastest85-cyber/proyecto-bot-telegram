"""
Configuración central del pipeline de descubrimiento de billeteras.
Rellena HELIUS_API_KEY con tu clave gratuita de https://dev.helius.xyz

v3: plan Helius de 10M créditos/mes. Se subió la profundidad de
análisis (más historial por billetera, más tokens por ciclo) y TODOS
los límites de consumo son configurables por variable de entorno, para
poder ajustarlos desde Railway sin redeploy si el gasto de créditos
sube demasiado. Vigila el consumo en el panel de Helius el primer día.
"""

def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default

import os

# ── Claves API ────────────────────────────────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "TU_CLAVE_AQUI")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_PARSED_TX = "https://api.helius.xyz/v0/addresses/{address}/transactions"

# DexScreener no requiere clave (API pública con rate limit ~300 req/min)
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# GeckoTerminal tampoco requiere clave (30 req/min en plan gratuito)
GECKO_TRENDING = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"

# ── Criterios para considerar un token "ganador" ─────────────────────────
MIN_PRICE_CHANGE_24H = _float("MIN_PRICE_CHANGE_24H", 150.0)  # % subida 24h
MIN_VOLUME_24H_USD = _int("MIN_VOLUME_24H_USD", 250_000)   # volumen mínimo
MIN_LIQUIDITY_USD = _int("MIN_LIQUIDITY_USD", 50_000)      # liquidez mínima
# Solo "ganadores" que llegaron LEJOS: MC/FDV mínimo. Un pump de 8k→30k no
# aporta billeteras copiables; uno que migró y llegó a cientos de miles sí.
MIN_MC_USD = _int("MIN_MC_USD", 500_000)
MAX_TOKEN_AGE_DAYS = _int("MAX_TOKEN_AGE_DAYS", 14)

# ── Presupuesto de Helius (plan 10M créditos/mes; todo tuneable por env) ──
# Si el consumo sube demasiado, baja estos valores desde Railway (variables
# de entorno) — no hace falta redeploy de código.
MAX_TOKENS_PER_CYCLE = _int("MAX_TOKENS_PER_CYCLE", 35)   # tokens por ciclo
HISTORY_MAX_PAGES = _int("HISTORY_MAX_PAGES", 20)         # páginas por token (100 txs c/u)
PROFILE_MAX_PAGES = _int("PROFILE_MAX_PAGES", 20)         # páginas al perfilar 1 billetera (~2000 txs)

# ── Criterios para considerar una billetera "interesante" ────────────────
# Ventana de OBSERVACIÓN: txs a leer por token. Grande para llegar más
# allá de la zona de snipers (no cuesta Helius extra: ya se paginaba igual).
EARLY_BUYER_WINDOW = _int("EARLY_BUYER_WINDOW", 1500)
# ── Embudo v4: observar ≠ perfilar ────────────────────────────────────
# OBSERVAR (barato, alimenta grafo/clusters/afinidad con devs): compras
# desde MIN_OBS_BUY_SOL hasta el rank BUYER_END_RANK.
# PERFILAR (caro, Helius+IA): solo candidatas FUERA de la zona de snipers
# (rank ≥ BUYER_START_RANK y ≥ MIN_BUY_DELAY_SEC tras la 1ª tx del token)
# con compras ≥ MIN_BUY_SOL. Los snipers/devs quedan en el grafo pero no
# gastan créditos de perfil.
MIN_OBS_BUY_SOL = _float("MIN_OBS_BUY_SOL", 0.3)
BUYER_START_RANK = _int("BUYER_START_RANK", 30)
BUYER_END_RANK = _int("BUYER_END_RANK", 600)
MIN_BUY_DELAY_SEC = _int("MIN_BUY_DELAY_SEC", 60)
MIN_BUY_SOL = _float("MIN_BUY_SOL", 1.0)   # compra mínima para ser candidata
MAX_BUY_SOL = _float("MAX_BUY_SOL", 300)   # ignorar ballenas/market makers
# Pesos del score de descubrimiento (pre-filtro; el PnL decide al final):
# capital real comprometido, reincidencia en ganadores, y algo de rank.
W_CAPITAL = _int("W_CAPITAL", 40)
W_REPEAT = _int("W_REPEAT", 45)
W_RANK = _int("W_RANK", 15)
# Ganancia REALIZABLE: el valor de una posición en cartera se topa a esta
# fracción de la liquidez del pool (no podrías vender más sin hundirlo).
LIQ_CAP_FRACTION = _float("LIQ_CAP_FRACTION", 0.10)
# Nº mínimo de tokens ganadores para ser CANDIDATA a ⭐. Antes 2 (muy
# exigente: casi nadie coincide en 2 memecoins). Ahora 1 + la rentabilidad
# decide (la IA/grading filtran). Tuneable por env.
MIN_WINNING_TOKENS = _int("MIN_WINNING_TOKENS", 1)
# Tope de billeteras marcadas ⭐ por ciclo (las mejores por score de
# descubrimiento). Protege el webhook de Helius y el coste de evaluación.
MAX_TRACKED_CANDIDATES = _int("MAX_TRACKED_CANDIDATES", 60)
# Tope de billeteras que la IA perfila+evalúa por ciclo (coste Helius+IA).
MAX_EVAL_PER_CYCLE = _int("MAX_EVAL_PER_CYCLE", 8)
# Presupuesto de atención: máximo de ⭐ en seguimiento activo. Si se supera,
# las de menor Priority Score descienden. Mantiene el sistema ordenado.
MAX_ELITE = _int("MAX_ELITE", 500)
# Presupuesto DIARIO de llamadas a la IA. Al agotarse, la clasificación cae
# al respaldo por grading (sin IA) y se reanuda al día siguiente. Evita que
# el coste de IA se dispare. Súbelo/bájalo desde Railway.
AI_DAILY_BUDGET = _int("AI_DAILY_BUDGET", 300)
# Escalado a modelo potente (Sonnet) cuando Haiku duda. Off por defecto para
# ahorrar: Haiku basta para clasificar. Pon AI_ESCALATE=1 para activarlo.
AI_ESCALATE = os.getenv("AI_ESCALATE", "0") == "1"
BOT_TX_PER_HOUR_LIMIT = 60        # más de esto por hora = probable bot

# ── Base de datos ─────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "wallets.db")

# ── Rate limiting (respetar planes gratuitos) ─────────────────────────────
GECKO_DELAY = 2.5                 # segundos entre requests a GeckoTerminal
# El delay es por límite de RPS del plan, no por créditos. 0.1s ≈ 10 req/s.
HELIUS_DELAY = float(os.getenv("HELIUS_DELAY", "0.1"))
DEXSCREENER_DELAY = 0.3
