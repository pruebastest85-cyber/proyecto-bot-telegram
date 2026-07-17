"""
Configuración central del pipeline de descubrimiento de billeteras.
Rellena HELIUS_API_KEY con tu clave gratuita de https://dev.helius.xyz

v2: calibrada para acelerar la toma de datos SIN agotar la cuota
gratuita de Helius (~500k créditos/mes ≈ 160 consultas de historial
al día). El control clave es MAX_TOKENS_PER_CYCLE.
"""

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
MIN_PRICE_CHANGE_24H = 150.0      # % mínimo de subida en 24h
MIN_VOLUME_24H_USD = 100_000      # volumen mínimo para descartar tokens muertos
MIN_LIQUIDITY_USD = 20_000        # liquidez mínima para descartar rug pulls obvios
MAX_TOKEN_AGE_DAYS = 14           # solo tokens recientes (memecoins nuevos)

# ── Presupuesto de Helius (para no agotar la cuota del mes) ───────────────
# Plan gratuito: ~1M créditos/mes ≈ 330 consultas de historial al día.
MAX_TOKENS_PER_CYCLE = 18         # tokens analizados por ciclo; el resto en cola
HISTORY_MAX_PAGES = 14            # páginas de historial por token (100 txs c/u)

# ── Criterios para considerar una billetera "interesante" ────────────────
EARLY_BUYER_WINDOW = 200          # nº de primeras transacciones a analizar por token
MIN_BUY_SOL = 0.5                 # ignorar compras de prueba menores a esto
MAX_BUY_SOL = 500                 # ignorar market makers / billeteras enormes
MIN_WINNING_TOKENS = 2            # billetera debe aparecer en ≥2 tokens ganadores
BOT_TX_PER_HOUR_LIMIT = 60        # más de esto por hora = probable bot

# ── Base de datos ─────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "wallets.db")

# ── Rate limiting (respetar planes gratuitos) ─────────────────────────────
GECKO_DELAY = 2.5                 # segundos entre requests a GeckoTerminal
HELIUS_DELAY = 0.12               # plan Developer: 50 RPS; vamos holgados a ~8
DEXSCREENER_DELAY = 0.3
