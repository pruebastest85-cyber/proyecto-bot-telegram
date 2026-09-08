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
# (19-E) El defecto era la cadena "TU_CLAVE_AQUI", que es TRUTHY y ESTA
# EN EL REPO PUBLICO. Dos consecuencias, las dos reales:
#   · `if not config.HELIUS_API_KEY` (laserstream) era codigo muerto:
#     nunca podia disparar, asi que LaserStream intentaba conectar con
#     una clave falsa y todo daba 401 en silencio.
#   · La ruta `/helius` autenticaba comparando contra ESA cadena, o sea
#     con una contraseña publica: cualquiera que alcanzara el puerto
#     podia inyectar transacciones falsas que acaban en `signals`,
#     `positions` y `paper_trades` como si estuvieran medidas.
# Vacio es falsy: el corte de LaserStream vuelve a funcionar y el
# webhook rechaza todo (ver `realtime.helius_hook`).
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
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
# (19-K) Con EVAL_ADAPTATIVO=1 esto pasa a ser el SUELO, no el tope: el
# cupo real de cada ciclo lo calcula `ai_analyst.cupo_evaluaciones` con
# el presupuesto de Helius que queda. El porqué, ahí.
MAX_EVAL_PER_CYCLE = _int("MAX_EVAL_PER_CYCLE", 15)
# (19-K) Cupo de perfilado ajustado al presupuesto que queda del ciclo.
# 0 = volver al número fijo de MAX_EVAL_PER_CYCLE.
EVAL_ADAPTATIVO = _int("EVAL_ADAPTATIVO", 1)
# Techo duro del cupo por ciclo, pase lo que pase con el presupuesto.
# Existe para que un error de cálculo no lance miles de perfilados de
# golpe: ese gasto sería irreversible.
EVAL_MAX_POR_CICLO = _int("EVAL_MAX_POR_CICLO", 400)
# Suelo del cupo cuando el presupuesto da para ello. Evita que un ciclo
# se quede en 1-2 perfilados y el embudo se pare.
EVAL_MIN_POR_CICLO = _int("EVAL_MIN_POR_CICLO", 10)
# Créditos que cuesta perfilar UNA billetera. Solo se usa mientras no
# haya datos del ciclo para medirlo de verdad. Medido el 30/8 sobre
# 7.798 perfilados reales: 279 (incluye tiempo real y radar, así que
# sobreestima). Se deja en 300 por el lado seguro.
EVAL_COSTE_CREDITOS = _int("EVAL_COSTE_CREDITOS", 300)
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
# (19-O) PROFIT FACTOR minimo: por cada SOL perdido, cuantos ganados.
# 0 = puerta APAGADA (el defecto: desplegar no cambia a quien copia el
# bot de nadie).
#
# Faltaba: la 19-N añadio la puerta en `filtro_calidad._cfg()`, que lee
# los ajustes con `getattr(config, ...)`, pero NO declaro la variable
# aqui. Resultado: `FILTRO_PF_MIN` en el entorno no llegaba a ninguna
# parte y la puerta era imposible de encender — un mando muerto que
# ademas parecia funcionar. Se vio al medir en la PC del dueño: con
# FILTRO_PF_MIN=2 exportado, `_cfg()` seguia diciendo pf_min=0.
#
# Es el MISMO fallo que la 19-L (AUTO_CYCLE_HOURS leido de `config`
# donde no estaba). Por eso la prueba que lo acompaña no comprueba solo
# este mando: recorre TODOS los ajustes que `filtro_calidad` espera y
# exige que existan aqui.
FILTRO_PF_MIN = _float("FILTRO_PF_MIN", 0.0)
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


# ══════════════════════════════════════════════════════════════════════
# EMBUDO v2 — fuente UNICA de configuracion del embudo nuevo
# ══════════════════════════════════════════════════════════════════════
# (Fase 3, 08/09/2026) Objetivo del dueño: encontrar traders humanos
# copiables a partir de tokens que demostraron crecimiento REAL y
# supervivencia, no francotiradores de lanzamientos. Nada de lo de abajo
# cambia el comportamiento del bot mientras EMBUDO_V2_ACTIVO valga 0:
# las fases 4-9 lo iran encendiendo por partes.
#
# Por que un bloque unico: hoy los umbrales del embudo estan repartidos
# entre config.py, grading.py, filtro_calidad.py y variables de entorno
# leidas a pelo en dos modulos. La regla 40 del encargo pide una sola
# fuente.

# Interruptor maestro. 0 = el bot se comporta EXACTAMENTE como hoy.
EMBUDO_V2_ACTIVO = _int("EMBUDO_V2_ACTIVO", 0)
# Version de la configuracion, para `analysis_events.config_version`:
# permite saber con que reglas se tomo cada decision.
CONFIG_VERSION = os.getenv("CONFIG_VERSION", "v2.0-fase3")

# ── Token ganador ─────────────────────────────────────────────────────
# SURVIVOR: llego a MIN_WINNER_MC y sigue sano. BREAKOUT: llego a
# BREAKOUT_MC. Un token de 500K que nunca llego a 1M NO se descarta
# (regla 3 del encargo): son dos grupos, no una escalera.
MIN_WINNER_MC = _int("MIN_WINNER_MC", 500_000)
BREAKOUT_MC = _int("BREAKOUT_MC", 1_000_000)


def _lista_num(name, default):
    """Lista de numeros desde una variable de entorno separada por comas.
    Un valor ilegible cae a la lista por defecto: este archivo lo importa
    el repo entero y un ValueError aqui impide arrancar (leccion 19-A)."""
    crudo = os.getenv(name)
    if not crudo:
        return list(default)
    try:
        vals = [float(x) for x in crudo.split(",") if x.strip()]
        return vals or list(default)
    except (TypeError, ValueError):
        print(f"· {name}={crudo!r} no es una lista de numeros; se usa "
              f"{default}")
        return list(default)


# Niveles de MC cuyo primer cruce se registra en `token_milestones`.
TOKEN_MILESTONES_USD = _lista_num(
    "TOKEN_MILESTONES_USD",
    [100_000, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000,
     10_000_000])

# ── Supervivencia ─────────────────────────────────────────────────────
# "Sigue vivo" NO es "current_mc > 0" (regla 4). El score 0-100 combina
# las señales observables; estos son los minimos por debajo de los
# cuales un token cuenta como abandonado.
SURVIVAL_24H_ENABLED = _int("SURVIVAL_24H_ENABLED", 1)
SURVIVAL_7D_ENABLED = _int("SURVIVAL_7D_ENABLED", 1)
SURVIVAL_MIN_LIQ_USD = _int("SURVIVAL_MIN_LIQ_USD", 15_000)
SURVIVAL_MIN_VOL24_USD = _int("SURVIVAL_MIN_VOL24_USD", 25_000)
# Cuanto puede haber caido desde su maximo sin considerarlo muerto.
SURVIVAL_MIN_PCT_ATH = _float("SURVIVAL_MIN_PCT_ATH", 10.0)

# ── Holder (lo que el dueño busca) ────────────────────────────────────
# Vara principal: mantener la posicion mas de un dia.
HOLD_MIN_HOURS = _float("HOLD_MIN_HOURS", 24.0)
# Posicion CERRADA = vendio entre el 70% y el 105% de los tokens que
# compro. Es la definicion mas estricta de las cuatro que hay hoy en el
# repo y la unica que evita contar una venta de polvo como cierre; a
# partir de la fase 6 sera la UNICA (decision del dueño, 08/09).
POSICION_CERRADA_MIN_FRAC = _float("POSICION_CERRADA_MIN_FRAC", 0.70)
POSICION_CERRADA_MAX_FRAC = _float("POSICION_CERRADA_MAX_FRAC", 1.05)
# Profit factor sin perdidas: hoy hay tres centinelas distintos (99.99 en
# wallet_metrics y filtro_calidad, 9.99 en copiabilidad). Regla 17: un
# tope configurable, y NULL con motivo cuando ni eso tiene sentido.
PF_MAX = _float("PF_MAX", 20.0)

# ── Presupuesto de Helius por categorias (reglas 8 y 9) ───────────────
# HELIUS_MONTHLY_BUDGET es el mismo numero que HELIUS_MONTHLY_CREDITS;
# se nombra aparte porque el encargo lo pide asi y porque el ledger v2
# reparte ESTE total entre buckets.
HELIUS_MONTHLY_BUDGET = _int("HELIUS_MONTHLY_BUDGET", HELIUS_MONTHLY_CREDITS)
HELIUS_DISCOVERY_BUDGET_PCT = _int("HELIUS_DISCOVERY_BUDGET_PCT", 30)
HELIUS_WALLET_BUDGET_PCT = _int("HELIUS_WALLET_BUDGET_PCT", 40)
HELIUS_DEEP_BUDGET_PCT = _int("HELIUS_DEEP_BUDGET_PCT", 20)
HELIUS_RESERVE_BUDGET_PCT = _int("HELIUS_RESERVE_BUDGET_PCT", 10)
# Coste REAL por llamada segun la documentacion de Helius (verificada el
# 08/09/2026). El contador actual cobra 10 al RPC estandar, que cuesta 1.
HELIUS_COSTE_RPC_ESTANDAR = _int("HELIUS_COSTE_RPC_ESTANDAR", 1)
HELIUS_COSTE_DAS = _int("HELIUS_COSTE_DAS", 10)
HELIUS_COSTE_ENHANCED = _int("HELIUS_COSTE_ENHANCED", 100)
HELIUS_COSTE_WALLET_API = _int("HELIUS_COSTE_WALLET_API", 100)
# WebSocket: 2 creditos por cada 0,1 MB de datos sin comprimir. Hoy no se
# cuenta NADA de esto y por eso el panel de Helius y el contador propio
# no cuadran.
HELIUS_COSTE_WS_POR_MB = _float("HELIUS_COSTE_WS_POR_MB", 20.0)

# ── Historial de mercado del token (fase 4) ───────────────────────────
# Solo usa DexScreener (gratis) y el historial de MC que ya esta en
# `signals`: CERO creditos de Helius. Con 30 tokens por pasada es UNA
# peticion cada media hora, frente a las miles que el bot ya hace.
TOKEN_HISTORY_ACTIVO = _int("TOKEN_HISTORY_ACTIVO", 1)
TOKEN_HISTORY_TOKENS_POR_PASADA = _int("TOKEN_HISTORY_TOKENS_POR_PASADA", 30)
TOKEN_HISTORY_BACKFILL_POR_PASADA = _int(
    "TOKEN_HISTORY_BACKFILL_POR_PASADA", 25)
# Por debajo de esta nota (0-100) un token no cuenta como superviviente.
SURVIVAL_SCORE_MIN = _float("SURVIVAL_SCORE_MIN", 50.0)

# ── Cordura del market cap (19-BB) ────────────────────────────────────
# El historico de `signals` trae MC contaminados por el fallo del precio
# ajeno que se corrigio el 07/09 (19-AX): antes, si DexScreener devolvia
# un par donde NUESTRO token era la moneda de cotizacion, se guardaba el
# precio y el MC del OTRO token. Medido en la base: 109 señales con MC
# >= 1.000 M, entre ellas un "token" de 13,8 BILLONES con 76 M de
# liquidez (x182.253). Sin filtro, esas filas dan maximos historicos
# falsos y convierten cualquier token en BREAKOUT.
#
# Los dos topes salen de la distribucion real: el percentil 99,9 del MC
# es 250 M y los contaminados empiezan en 1.000 M con relaciones de
# x95.000 para arriba. Un token de verdad ronda 5-100 veces su liquidez.
MC_MAX_CREIBLE = _float("MC_MAX_CREIBLE", 20_000_000_000.0)
MC_LIQ_RATIO_MAX = _float("MC_LIQ_RATIO_MAX", 10_000.0)

# ── Reconstruccion de posiciones (fase 5) ─────────────────────────────
# Junta compra y venta en UNA fila por (billetera, token) para saber
# cuanto gano de verdad y cuanto tiempo aguanto. Se alimenta de `trades`,
# el historial propio que el perfilador lleva guardando desde diciembre:
# CERO creditos de Helius. Se apaga con POSICIONES_ACTIVO=0.
POSICIONES_ACTIVO = _int("POSICIONES_ACTIVO", 1)
# Tokens por pasada. Es lectura de `trades` y una escritura por
# billetera; 20 tokens son segundos de trabajo y evitan tener la base
# ocupada mucho rato con el bot corriendo.
POSICIONES_TOKENS_POR_PASADA = _int("POSICIONES_TOKENS_POR_PASADA", 20)
# Tope de filas del registro auditable de decisiones. La base viaja en el
# backup de Telegram: ninguna tabla puede crecer sin freno.
ANALYSIS_EVENTS_MAX = _int("ANALYSIS_EVENTS_MAX", 50_000)

# ── Nota de calidad de la billetera (fase 6) ──────────────────────────
# Junta TODAS las posiciones medidas de una billetera y contesta si
# aguanta y gana o solo voltea. Vive en las columnas `q_*` y NO decide
# nada todavia: la nota vieja (`score`) sigue mandando. Cero creditos.
CALIDAD_ACTIVO = _int("CALIDAD_ACTIVO", 1)
# Con una sola operacion no se distingue el acierto de la suerte. Tres es
# el minimo con el que hoy hay material: 256 billeteras lo cumplen.
CALIDAD_MIN_POSICIONES = _int("CALIDAD_MIN_POSICIONES", 3)
CALIDAD_WALLETS_POR_PASADA = _int("CALIDAD_WALLETS_POR_PASADA", 200)
# Cada cuantas horas se vuelve a puntuar a una que ya tiene nota.
CALIDAD_REFRESCO_H = _int("CALIDAD_REFRESCO_H", 24)
# Pesos de las cinco patas. NO tienen por que sumar 100: la nota es una
# media ponderada, asi que lo que importa es la proporcion entre ellos —
# y cuando una pata no se puede medir, su peso se reparte entre las
# demas en vez de contar como cero. Aguantar pesa mas que nada porque es
# el criterio que el dueño puso por escrito.
Q_PESO_HOLD = _float("Q_PESO_HOLD", 30.0)
Q_PESO_CONSISTENCIA = _float("Q_PESO_CONSISTENCIA", 25.0)
Q_PESO_PROFIT = _float("Q_PESO_PROFIT", 25.0)
Q_PESO_SUPERVIVENCIA = _float("Q_PESO_SUPERVIVENCIA", 10.0)
Q_PESO_RIESGO = _float("Q_PESO_RIESGO", 10.0)

# ── Libro de cuentas de Helius (fase 7) ───────────────────────────────
# Apunta EN QUE se gasta cada credito (por dia, endpoint y sobre), que
# hoy no se sabe: todo cae en un contador diario suelto. Se apaga con
# HELIUS_LEDGER_ACTIVO=0. No cambia nada de lo que el bot gasta: solo
# deja constancia. El freno del 85 % sigue siendo el de siempre.
HELIUS_LEDGER_ACTIVO = _int("HELIUS_LEDGER_ACTIVO", 1)
# Filas conservadas. Son agregados por dia/endpoint/sobre (~20 al dia),
# asi que 20.000 son casi tres años de historial.
HELIUS_LEDGER_MAX = _int("HELIUS_LEDGER_MAX", 20_000)

# ── Descubrimiento dirigido (fase 8) ──────────────────────────────────
# Sale a buscar billeteras NUEVAS con el criterio del dueño: no quien
# gano mas, sino quien MULTIPLICA metiendo poco capital. Se apaga con
# DESCUBRIMIENTO_ACTIVO=0.
DESCUBRIMIENTO_ACTIVO = _int("DESCUBRIMIENTO_ACTIVO", 1)
# La banda de capital sale de la distribucion REAL de las 2.555
# posiciones medidas: por debajo de 0,5 SOL solo el 37 % acaba en
# ganancia (es polvo, y ademas dispara multiplos falsos: hay un caso de
# "x1.358" que metio 0,004 SOL); entre 1 y 3 SOL esta la cola gorda
# (percentil 90 en x8,5, uno de cada cuatro pasa de x3); por encima de
# 30 SOL se gana a menudo pero se multiplica x2,6, que es lo contrario
# de lo que se busca.
DESCUBRIMIENTO_MIN_SOL = _float("DESCUBRIMIENTO_MIN_SOL", 0.5)
DESCUBRIMIENTO_MAX_SOL = _float("DESCUBRIMIENTO_MAX_SOL", 5.0)
DESCUBRIMIENTO_TOKENS_POR_PASADA = _int(
    "DESCUBRIMIENTO_TOKENS_POR_PASADA", 3)
DESCUBRIMIENTO_CANDIDATOS_POR_TOKEN = _int(
    "DESCUBRIMIENTO_CANDIDATOS_POR_TOKEN", 10)
DESCUBRIMIENTO_PERFILES_POR_PASADA = _int(
    "DESCUBRIMIENTO_PERFILES_POR_PASADA", 25)
# TOPE DURO de creditos por pasada, comprobado contra el libro de la
# fase 7. Con ~200 por token y ~230 por billetera, una pasada normal
# ronda los 7.500: 30.000 deja margen de sobra y aun asi hace imposible
# repetir lo del 3 de septiembre (2,4 millones en un dia).
DESCUBRIMIENTO_CREDITOS_POR_PASADA = _int(
    "DESCUBRIMIENTO_CREDITOS_POR_PASADA", 30_000)

# ── Replay de copia (regla 26) ────────────────────────────────────────
COPY_DELAY_TESTS = [int(x) for x in _lista_num(
    "COPY_DELAY_TESTS", [5, 15, 30, 60, 300])]

# ── Ciclo automatico: A PROPOSITO NO ESTA AQUI ────────────────────────
# `AUTO_CYCLE_HOURS` vive SOLO en el entorno y cada modulo lo lee con
# os.getenv EN EL MOMENTO DE USARLO. No es descuido: la ola 19-K lo leyo
# con getattr(config, "AUTO_CYCLE_HOURS", 6), el atributo no existia,
# siempre caia al 6 del defecto y con el ciclo real del dueño el cupo de
# evaluaciones salia TRIPLE (gasto de creditos irreversible). Hay una
# prueba que falla si alguien lo añade a este archivo. Centralizarlo mas
# adelante exige una FUNCION que lea el entorno al llamarla, no una
# constante congelada en el import.
