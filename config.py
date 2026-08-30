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
MAX_TOKENS_PER_CYCLE = _int("MAX_TOKENS_PER_CYCLE", 30)   # tokens por ciclo
# Con 20 páginas (2.000 txs) NO se llegaba al inicio de tokens concurridos:
# se leían las 2.000 MÁS RECIENTES y se trataban como si fueran las primeras,
# inventando puestos de compra. La cuota de Helius estaba al 4%, así que
# ahora se pagina mucho más hondo y además se detecta si se alcanzó el inicio.
HISTORY_MAX_PAGES = _int("HISTORY_MAX_PAGES", 60)         # páginas por token (100 txs c/u)
# El PnL es EL criterio para ser Elite, y se calculaba viendo solo las
# últimas 2.000 txs (13-40 días de un trader activo). Con 60 páginas se ven
# ~6.000 txs: PnL, drawdown y consistencia mucho más reales. La cuota de
# Helius estaba al 17%, así que este gasto es el que mejor se paga.
PROFILE_MAX_PAGES = _int("PROFILE_MAX_PAGES", 50)         # páginas al perfilar 1 billetera (~5000 txs)

# ── Criterios para considerar una billetera "interesante" ────────────────
# Ventana de OBSERVACIÓN: txs a leer por token. Grande para llegar más
# allá de la zona de snipers (no cuesta Helius extra: ya se paginaba igual).
EARLY_BUYER_WINDOW = _int("EARLY_BUYER_WINDOW", 2000)
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
# CRECIMIENTO DESDE LA ENTRADA: el filtro que de verdad separa a quien
# compró ANTES de que el token explotara de quien llegó tarde. Sustituye a
# la ventana de buy_rank, que era frágil (dependía de leer todo el historial)
# y no medía valoración. Un x3 significa que desde su compra el token
# triplicó: eso es anticipación real, no suerte de estar en la lista.
MIN_ENTRY_MULTIPLE = _float("MIN_ENTRY_MULTIPLE", 3.0)

MIN_BUY_SOL = _float("MIN_BUY_SOL", 1.0)   # compra mínima para ser candidata
MAX_BUY_SOL = _float("MAX_BUY_SOL", 300)   # ignorar ballenas/market makers
# Pesos del score de descubrimiento (pre-filtro; el PnL decide al final):
# capital real comprometido, reincidencia en ganadores, y algo de rank.
W_CAPITAL = _int("W_CAPITAL", 40)
W_REPEAT = _int("W_REPEAT", 45)
# El puesto de compra dependía de leer TODO el historial del token (frágil).
# Se sustituye por el múltiplo desde la entrada, que mide lo mismo (¿entró
# antes de que explotara?) de forma fiable y con la valoración real.
W_RANK = _int("W_RANK", 0)
W_ENTRY = _int("W_ENTRY", 15)   # crecimiento desde su entrada
# Ganancia REALIZABLE: el valor de una posición en cartera se topa a esta
# fracción de la liquidez del pool (no podrías vender más sin hundirlo).
LIQ_CAP_FRACTION = _float("LIQ_CAP_FRACTION", 0.10)
# Nº mínimo de tokens ganadores para ser CANDIDATA a ⭐. Antes 2 (muy
# exigente: casi nadie coincide en 2 memecoins). Ahora 1 + la rentabilidad
# decide (la IA/grading filtran). Tuneable por env.
# Aparecer en UN solo token ganador es compatible con la pura suerte. Exigir
# 2 es el filtro más barato y honesto para separar pericia de casualidad.
# Temporalmente en 1: con 2 el embudo se quedaba en 3 candidatas por ciclo.
# El grading (Profit Factor, drawdown, concentración) es ahora quien filtra
# la suerte. Subir a 2 cuando haya suficiente material acumulado.
MIN_WINNING_TOKENS = _int("MIN_WINNING_TOKENS", 1)
# Tope de billeteras marcadas ⭐ por ciclo (las mejores por score de
# descubrimiento). Protege el webhook de Helius y el coste de evaluación.
MAX_TRACKED_CANDIDATES = _int("MAX_TRACKED_CANDIDATES", 60)
# Tope de billeteras que la IA perfila+evalúa por ciclo (coste Helius+IA).
MAX_EVAL_PER_CYCLE = _int("MAX_EVAL_PER_CYCLE", 15)
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

# ── Creadores de mercado (26/8/2026, regla del dueño) ────────────────────
# Una billetera que compra y VENDE el MISMO token una y otra vez no está
# operando: está haciendo mercado (o lavando volumen). Gane o no gane, su
# forma de ganar NO se puede copiar — medido sobre las señales reales del
# bot, el grupo de 5+ vueltas tiene mediana -69,4% frente a -59,5% del
# resto, y mete 3,9 señales por token frente a 2,3.
# "Vuelta" = una ALTERNANCIA compra→venta del mismo token en orden
# temporal: compra-venta-compra-venta son 2 vueltas, pero 7 compras
# escalonadas seguidas de 5 ventas parciales es UNA sola (una posición
# que se abre y se cierra por partes, comportamiento normal de trader).
# MM_VUELTAS_MAX = 0 apaga la regla.
MM_VUELTAS_MAX = _int("MM_VUELTAS_MAX", 5)
MM_VENTANA_DIAS = _int("MM_VENTANA_DIAS", 30)

# ── Filtro de tres puertas (27/8/2026, ver filtro_calidad.py) ────────────
# La estrella se GANA: historial real (WR y cerradas), estrategia
# copiable (retención y diversificación) y confirmación con señales
# medidas por ESTE bot. FILTRO_TRES_PUERTAS=0 vuelve al comportamiento
# anterior (todas las ⭐ alertan).
FILTRO_TRES_PUERTAS = _int("FILTRO_TRES_PUERTAS", 1)
FILTRO_WR_MIN = _float("FILTRO_WR_MIN", 60.0)
FILTRO_MIN_CERRADAS = _int("FILTRO_MIN_CERRADAS", 10)
FILTRO_VENTANA_DIAS = _int("FILTRO_VENTANA_DIAS", 90)
FILTRO_HOLD_MIN_MIN = _float("FILTRO_HOLD_MIN_MIN", 30.0)
FILTRO_MIN_TOKENS = _int("FILTRO_MIN_TOKENS", 8)
FILTRO_MIN_MEDIDAS = _int("FILTRO_MIN_MEDIDAS", 5)
FILTRO_ACIERTO_MIN = _float("FILTRO_ACIERTO_MIN", 40.0)
FILTRO_PRUEBA_DIAS = _int("FILTRO_PRUEBA_DIAS", 14)
# Arranque elegido por el dueño (27/8): la que pasa historial+copiable
# alerta y se copia YA como "confirmada provisional", y pierde la
# confirmación si sus primeras FILTRO_MIN_MEDIDAS medidas salen malas.
# FILTRO_PROVISIONAL=0 = modo estricto (nadie alerta sin medidas buenas).
FILTRO_PROVISIONAL = _int("FILTRO_PROVISIONAL", 1)
# (18-N, cazado por el dueño) La puerta 1 exige ademas GANAR de verdad:
# neto en SOL de la ventana > FILTRO_NETO_MIN. El winrate de las cerradas
# escondia a billeteras que ganan poquitos y palman fuerte en bolsas que
# nunca cierran (caso real: 62% de acierto en cerradas y -21,8 SOL netos).
FILTRO_NETO_MIN = _float("FILTRO_NETO_MIN", 0.0)
# (18-O) La IA no puede DAR la estrella a quien no pasa las puertas 1-2.
# Medido en la base del dueño el 28/8: de 24 ⭐ vivas, solo 7 pasaban el
# embudo; las otras 17 las habia promovido la re-evaluacion de la IA en
# un dia, sin pasar por el filtro. Ninguna podia confirmarse jamas (la
# puerta 1 corta antes), asi que solo servian para inflar la lista y
# ocupar sitio en el webhook. OJO: esto IMPIDE DARLA de aqui en
# adelante; a las ⭐ que ya estan puestas las retira /reembudo, que es un
# mando manual a proposito. FILTRO_PUERTA_PROMOCION=0 lo apaga.
FILTRO_PUERTA_PROMOCION = _int("FILTRO_PUERTA_PROMOCION", 1)

# ── Base de datos ─────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "wallets.db")

# ── Rate limiting (respetar planes gratuitos) ─────────────────────────────
GECKO_DELAY = 2.5                 # segundos entre requests a GeckoTerminal
# El delay es por límite de RPS del plan, no por créditos. 0.1s ≈ 10 req/s.
# ⚠️ COSTE REAL: la Enhanced Transactions API de Helius cuesta 100 CRÉDITOS
# por llamada (no 1). Con 10M créditos/mes son ~100.000 llamadas al mes.
# Los topes de arriba son GENEROSOS a propósito: en la práctica el embudo
# consume mucho menos (el límite real es cuántos tokens encuentra, no la
# configuración). La protección de verdad es helius_budget, que mide el
# consumo real y pausa las descargas al llegar a HELIUS_STOP_AT_PCT.
# Docs: helius.dev/docs/billing/credits
# getTransactionsForAddress: 10 créditos por 100 txs (10x más barato que la
# Enhanced API) y hasta 1.000 por llamada, con sortOrder=asc para alcanzar
# de verdad el inicio del token. Poner USE_RPC_HISTORY=0 para volver atrás.
USE_RPC_HISTORY = os.getenv("USE_RPC_HISTORY", "1") == "1"

HELIUS_CREDITS_PER_CALL = _int("HELIUS_CREDITS_PER_CALL", 100)
# (Ola 17-B) Coste de una llamada RPC estandar (getTokenAccountsByOwner,
# getAsset...). Helius cobra ~10 creditos por estas; se nombra aqui para
# que el contador de creditos deje de ignorarlas.
HELIUS_CREDITS_RPC = _int("HELIUS_CREDITS_RPC", 10)
HELIUS_MONTHLY_CREDITS = _int("HELIUS_MONTHLY_CREDITS", 10_000_000)
HELIUS_STOP_AT_PCT = _int("HELIUS_STOP_AT_PCT", 85)   # pausar al 85%

# (19-A) Era la UNICA linea de este archivo que convertia a pelo. Y este
# archivo lo importa el repo ENTERO, asi que un `HELIUS_DELAY=0,1` en el
# .env lanzaba ValueError EN EL IMPORT: no arranca nada, y el supervisor
# entra en bucle de reinicios sin rollback. Ahora cae al defecto.
HELIUS_DELAY = _float("HELIUS_DELAY", 0.1)
DEXSCREENER_DELAY = 0.3
