"""
Bot de Telegram — Panel de control del pipeline de billeteras.
v5: HUB de navegación con menú de botones inline (se edita en el mismo
mensaje), secciones agrupadas, botones « Inicio / Cancelar y flujo
"pídeme la dirección" para los comandos que necesitan un <address>.
Mantiene TODO lo de v4 (alias IA, PnL, /top con botones, chat libre,
jobs automáticos, Mini App /app).

Variables de entorno:
  TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, HELIUS_API_KEY, DB_PATH
  AUTO_CYCLE_HOURS (opcional, default 6), PUBLIC_URL (para /app)
"""

import asyncio
import os
import re
import threading
import time as _t

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      WebAppInfo, BotCommand)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
from db import get_conn, wallet_evidence
from discovery import run_discovery
from wallet_analyzer import run_analysis
from wallet_profiler import profile_wallet, format_profile
from wallet_admin import (discard_wallet, restore_wallet, build_top_message)
from realtime import start_webhook_server, sync_helius_webhook

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_ADMIN_ID_RAW = os.getenv("TELEGRAM_ADMIN_ID")
if not _ADMIN_ID_RAW:
    raise RuntimeError(
        "Falta TELEGRAM_ADMIN_ID: sin este ID cualquier usuario puede "
        "ejecutar comandos de administrador. Configúralo como variable de entorno."
    )
try:
    ADMIN_ID = int(_ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        f"TELEGRAM_ADMIN_ID no es un entero válido: {_ADMIN_ID_RAW!r}"
    )
# Un 0 explícito reabriría el bypass: el decorador solo_admin comprueba
# `if ADMIN_ID and ...`, y 0 es falsy, así que no bloquearía a nadie.
if ADMIN_ID <= 0:
    raise RuntimeError(
        f"TELEGRAM_ADMIN_ID debe ser un ID de Telegram positivo, no "
        f"{ADMIN_ID}. Con 0 el filtro de administrador queda desactivado."
    )
AUTO_CYCLE_HOURS = float(os.getenv("AUTO_CYCLE_HOURS", "6"))

# Evita que el ciclo automático y un comando manual corran a la vez
cycle_lock = threading.Lock()

# Acciones del agente pendientes de confirmación (una por usuario)
PENDING_ACTIONS: dict[int, dict] = {}

# Usuarios a los que el hub les pidió un dato (address o pregunta).
# user_id -> nombre del comando ("perfil", "ficha", "preguntar"…)
AWAITING: dict[int, str] = {}

# Un contrato/mint de Solana pegado directo (base58, 32-44 chars)
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Tareas en segundo plano vivas (sin esta referencia el GC puede matarlas)
_BG_TASKS: set = set()


async def _send_md(chat, text, **kw):
    """Envía en Markdown; si Telegram lo rechaza (símbolos raros del token),
    reintenta en texto plano para NO perder el mensaje en silencio.
    Mismo criterio que realtime.tg_send."""
    try:
        return await chat.send_message(text, parse_mode="Markdown", **kw)
    except Exception as e:
        print(f"· Markdown rechazado ({e}); reintento en texto plano")
        try:
            return await chat.send_message(text, **kw)
        except Exception as e2:
            print(f"· No se pudo enviar el mensaje: {e2}")
            return None


def _token_keyboard(url, mint):
    """Botones bajo la ficha de token: DexScreener + 👍/👎 para aprender."""
    filas = []
    if url:
        filas.append([InlineKeyboardButton("📈 Ver en DexScreener", url=url)])
    filas.append([
        InlineKeyboardButton("👍 Buena", callback_data=f"tk:up:{mint}"),
        InlineKeyboardButton("👎 Mala", callback_data=f"tk:dn:{mint}")])
    return InlineKeyboardMarkup(filas)


# ─────────────────────────── HUB / MENÚ ────────────────────────────

def hub_text() -> str:
    return (
        "🔍 *Wallet Discovery Bot*\n"
        f"⚙️ Ciclo automático cada {AUTO_CYCLE_HOURS:g} h  ·  "
        "💬 escríbeme normal para chatear con la IA\n\n"
        "Elige una sección 👇"
    )


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _row_inicio() -> list:
    return [_btn("« Inicio", "h:home")]


def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("⚡ Acciones", "h:acciones"), _btn("🏆 Billeteras", "h:wallets")],
        [_btn("🔎 Analizar wallet", "h:consultar"), _btn("🤖 IA & Chat", "h:ia")],
        [_btn("📊 Estado", "h:run:status"), _btn("📱 Panel visual", "h:app")],
        [_btn("ℹ️ Todos los comandos", "h:help")],
    ])


def kb_acciones() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔄 Ciclo completo", "h:run:ciclo")],
        [_btn("🔍 Descubrir tokens", "h:run:descubrir")],
        [_btn("🧮 Analizar compradores", "h:run:analizar")],
        [_btn("🧪 Paper trading", "pap:ver")],
        _row_inicio(),
    ])


def kb_wallets() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🏆 Top 10", "h:run:top10"), _btn("🏆 Top 20", "h:run:top20")],
        [_btn("📡 Últimas señales", "h:run:senales")],
        _row_inicio(),
    ])


def kb_consultar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔬 Perfil", "h:ask:perfil"), _btn("🧮 Ficha / Score", "h:ask:ficha")],
        [_btn("🧠 Veredicto IA", "h:ask:ia"), _btn("📋 Evidencia", "h:ask:evidencia")],
        [_btn("❌ Descartar", "h:ask:descartar"), _btn("⭐ Rastrear", "h:ask:rastrear")],
        [_btn("📊 Rendimiento", "h:run:rendimiento"), _btn("🧪 Backtest", "h:run:backtest")],
        [_btn("💰 Saldos", "h:run:saldos"), _btn("🔗 Hermanas", "h:run:hermanas")],
        _row_inicio(),
    ])


def kb_ia() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("💬 Preguntar a la IA", "h:ask:preguntar")],
        _row_inicio(),
    ])


def kb_solo_inicio() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([_row_inicio()])


def kb_cancelar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("« Cancelar", "h:home")]])


def kb_paper() -> InlineKeyboardMarkup:
    """Botones bajo el resumen de /paper."""
    return InlineKeyboardMarkup([
        [_btn("🔄 Actualizar", "pap:ver"),
         _btn("🗑 Reiniciar", "pap:pedir")],
    ])


def kb_paper_confirmar(abiertas: int, cerradas: int) -> InlineKeyboardMarkup:
    """Segundo paso: borrar el historial es irreversible, así que el
    primer botón solo pregunta y este es el que ejecuta."""
    return InlineKeyboardMarkup([
        [_btn(f"⚠️ Sí, borrar {abiertas + cerradas}", "pap:reset")],
        [_btn("« No, dejarlo como está", "pap:ver")],
    ])


SECCIONES = {
    "acciones": ("⚡ *Acciones del pipeline*\n\nEjecuta tareas ahora mismo:",
                 kb_acciones),
    "wallets": ("🏆 *Billeteras*\n\nMira el ranking y las señales:", kb_wallets),
    "consultar": ("🔎 *Analizar una wallet*\n\nElige qué quieres ver; luego te "
                  "pediré la dirección:", kb_consultar),
    "ia": ("🤖 *IA & Chat*\n\nPregúntale a la IA sobre tu base, o simplemente "
           "escríbeme un mensaje normal y el agente responde o propone "
           "acciones:", kb_ia),
}

ASK_PROMPTS = {
    "perfil": "🔬 *Perfil de billetera*\nEnvíame la *dirección* de la wallet a investigar:",
    "ficha": "🧮 *Ficha / Wallet Score*\nEnvíame la *dirección* de la wallet:",
    "ia": "🧠 *Veredicto IA*\nEnvíame la *dirección* de la wallet:",
    "evidencia": "📋 *Evidencia*\nEnvíame la *dirección* de la wallet:",
    "descartar": "❌ *Descartar billetera*\nEnvíame la *dirección* a dejar de rastrear:",
    "rastrear": "⭐ *Rastrear billetera*\nEnvíame la *dirección* a revertir el descarte:",
    "preguntar": ("💬 *Preguntar a la IA*\nEscríbeme tu pregunta sobre la base "
                  "(top, señales, ROI, quién acumula…):"),
}

HELP_TEXT = (
    "ℹ️ *Todos los comandos*\n\n"
    "/ciclo — descubrimiento + análisis ahora\n"
    "/descubrir — buscar tokens ganadores\n"
    "/analizar — analizar compradores tempranos\n"
    "/top [n] — mejores billeteras (10/20/30) con ❌ para descartar\n"
    "/descartar <address> — dejar de rastrear una billetera\n"
    "/rastrear <address> — revertir un descarte\n"
    "/evidencia <address> — el porqué de una billetera\n"
    "/perfil <address> — investigar una billetera a fondo\n"
    "/ficha <address> — Wallet Score 0-100 con ROI y riesgo\n"
    "/preguntar <texto> — pregúntale a la IA sobre tu base\n"
    "/ia <address> — veredicto de la IA sobre una billetera\n"
    "/senales — últimas señales en tiempo real\n"
    "/status — estado de la base de datos\n"
    "/app — panel visual (Mini App)\n\n"
    "💬 También puedes escribirme normal (sin /) para chatear con el agente."
)


# ──────────────────── LÓGICA REUTILIZABLE (bloqueante) ─────────────────

def run_full_cycle() -> str:
    """Ejecuta descubrimiento + análisis. Devuelve resumen en texto."""
    if not cycle_lock.acquire(blocking=False):
        return "⏳ Ya hay un ciclo en curso; este intento se omitió."
    try:
        saved = run_discovery()
        run_analysis()
        conn = get_conn()
        wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
        tracked = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
        conn.close()
        hook_msg = sync_helius_webhook()
        print(f"📡 {hook_msg}")
        return (f"✅ Ciclo terminado.\n"
                f"Ganadores nuevos: {saved}\n"
                f"Billeteras totales: {wallets}\n"
                f"Rastreadas ⭐: {tracked}\n"
                f"📡 {hook_msg}")
    finally:
        cycle_lock.release()


def _status_text() -> str:
    conn = get_conn()
    try:
        tokens = conn.execute("SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"]
        pend = conn.execute(
            "SELECT COUNT(*) c FROM winning_tokens WHERE analyzed=0").fetchone()["c"]
        wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
        tracked = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
        descartadas = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_bot=1").fetchone()["c"]
        from db import get_setting
        umbral = get_setting(conn, "min_signal_score", "0")
        # (Ola 8, 21/8) Antes se mostraban los contadores funnel_profiled/
        # funnel_promoted, que suman EVALUACIONES de por vida (la misma
        # billetera cuenta N veces al re-evaluarse): decian "484/6800" con
        # 72 seguidas reales. Ahora: billeteras DISTINTAS, en vivo.
        prof = conn.execute(
            "SELECT COUNT(*) c FROM wallets "
            "WHERE ai_class IS NOT NULL").fetchone()["c"]
        prom = conn.execute(
            "SELECT COUNT(*) c FROM wallets "
            "WHERE ai_follow=1").fetchone()["c"]
        try:
            from api_usage import usage_line, flush as _api_flush
            _api_flush()
            apis = usage_line(conn)
        except Exception:
            apis = ""
    finally:
        conn.close()
    return (
        f"📊 *Estado del sistema*\n\n"
        f"⚙️ Ciclo automático: cada {AUTO_CYCLE_HOURS:g} h\n"
        f"🎯 Umbral de señal: {float(umbral or 0):.0f}/100\n"
        f"Tokens ganadores: {tokens} ({pend} pendientes)\n"
        f"Billeteras registradas: {wallets}\n"
        f"Billeteras rastreadas ⭐: {tracked}\n"
        f"Descartadas/bots ❌: {descartadas}"
        + (f"\n📈 Embudo: {prof} evaluadas → {prom} seguidas "
           f"({100 * prom / prof:.0f}%)" if prof else "")
        + (f"\n{apis}" if apis else ""))


def _senales_text() -> str:
    conn = get_conn()
    # (Ola 8, 21/8) El feed guarda señales de ⭐ Y de candidatas en
    # observación (liga de ascenso): se marca cuál es cuál en vez de
    # presentarlo todo como si fuera de ⭐.
    rows = conn.execute(
        "SELECT s.*, COALESCE(w.is_tracked, 0) es_estrella FROM signals s "
        "LEFT JOIN wallets w ON w.address = s.wallet "
        "ORDER BY s.ts DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        return ("📡 *Últimas señales*\n\n"
                "_Aún no hay señales. Llegarán cuando una billetera ⭐ "
                "compre o venda algo._")
    lines = ["📡 *Últimas señales*  _(⭐ rastreada · 👁 en observación)_",
             "━━━━━━━━━━━━━━", ""]
    for s in rows:
        # (Ola 8) minutos enteros con acarreo: 59,6 min ya no es "60 min"
        mins = int(round((_t.time() - s["ts"]) / 60))
        cuando = (f"hace {mins/60:.1f}h" if mins >= 60
                  else f"hace {mins} min")
        try:
            side = s["side"] or "compra"
        except (KeyError, IndexError):
            side = "compra"
        emoji = "🟢" if side == "compra" else "🔴"
        verbo = "compra" if side == "compra" else "venta"
        try:
            simbolo = s["symbol"] or (s["mint"][:10] + "…")
        except (KeyError, IndexError):
            simbolo = s["mint"][:10] + "…"
        simbolo = (str(simbolo).replace("*", "").replace("_", " ")
                   .replace("`", "").replace("[", ""))
        res = ""
        try:
            partes = []
            if s["chg_1h"] is not None:
                partes.append(f"1h {s['chg_1h']:+.0f}%")
            if s["chg_24h"] is not None:
                partes.append(f"24h {s['chg_24h']:+.0f}%")
            if partes:
                res = "\n    📈 " + "  ·  ".join(partes)
        except (KeyError, IndexError):
            pass
        try:
            marca = "⭐" if s["es_estrella"] else "👁"
        except (KeyError, IndexError):
            marca = ""
        lines.append(
            f"{emoji} *{simbolo}*  ·  {verbo}  ·  {s['sol']:.2f} SOL"
            f"  {marca}\n    🕒 {cuando}{res}\n")
    return "\n".join(lines).rstrip()


def _evidencia_text(address: str) -> str:
    conn = get_conn()
    rows = wallet_evidence(conn, address)
    conn.close()
    if not rows:
        return "Sin registros para esa dirección."
    lines = [f"📋 *Evidencia de* `{address[:20]}…`:\n"]
    for ev in rows:
        lines.append(f"• {ev['reason']}\n")
    lines.append(f"\n🔗 Verificar: gmgn.ai/sol/address/{address}")
    return "\n".join(lines)


def _ficha_text(address: str):
    from wallet_score import compute_score, format_ficha
    from signal_tracker import wallet_track_record, format_track_record
    p = profile_wallet(address)
    if not p["tx_sampled"]:
        return None
    conn = get_conn()
    track = wallet_track_record(conn, address)
    row = conn.execute("SELECT alias FROM wallets WHERE address=?",
                       (address,)).fetchone()
    conn.close()
    s = compute_score(p, track)
    alias = row["alias"] if row and row["alias"] else None
    ficha = format_ficha(address, s, alias, format_track_record(track))
    # Saldo actual on-chain
    try:
        import requests as _rq
        resp = _rq.post(config.HELIUS_RPC,
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": "getBalance", "params": [address]},
                        timeout=15)
        sol = resp.json()["result"]["value"] / 1e9
        ficha += f"\n💰 Saldo actual: *{sol:,.2f} SOL*"
    except Exception:
        pass
    return ficha


def _ia_text(address: str) -> str:
    from ai_analyst import ai_verdict
    p = profile_wallet(address)
    if not p["tx_sampled"]:
        return "Sin transacciones para esa dirección."
    conn = get_conn()
    ev = conn.execute(
        "SELECT reason FROM appearances WHERE wallet=? LIMIT 6",
        (address,)).fetchall()
    conn.close()
    v = ai_verdict(p, [e["reason"] for e in ev])
    if not v:
        return "La IA no devolvió veredicto (¿ANTHROPIC_API_KEY configurada?)."
    icono = "✅ SEGUIR" if v["seguir"] else "❌ DESCARTAR"
    alias_txt = f"Alias: 👤 *{v['alias']}*\n" if v.get("alias") else ""
    return (f"🧠 *Veredicto IA para* `{address[:16]}…`\n\n"
            f"{alias_txt}"
            f"Clasificación: *{v['clasificacion'].upper()}*\n"
            f"Recomendación: {icono}\n"
            f"Confianza: {v.get('confianza', '?')}%\n\n"
            f"_{v.get('razon', '')}_")


def app_keyboard():
    """Teclado con el botón de la Mini App, o None si falta PUBLIC_URL."""
    public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    public_url = public_url.removeprefix("https://").removeprefix("http://")
    if not public_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📊 Abrir panel", web_app=WebAppInfo(url=f"https://{public_url}/app"))]])


# ──────────────── DISPATCHERS ASÍNCRONOS (comando + hub) ───────────────

async def run_address_command(chat, cmd: str, arg: str):
    """Ejecuta un comando que necesita un dato (address o pregunta).
    Usado tanto por los /comandos como por el flujo de botones del hub."""
    arg = (arg or "").strip()
    if cmd == "perfil":
        await chat.send_message("🔬 Investigando billetera… (30-60 segundos)")
        p = await asyncio.to_thread(profile_wallet, arg)
        await chat.send_message(format_profile(p), parse_mode="Markdown")
    elif cmd == "ficha":
        await chat.send_message("🧮 Calculando Wallet Score… (~1 min)")
        ficha = await asyncio.to_thread(_ficha_text, arg)
        await chat.send_message(ficha or "Sin transacciones para esa dirección.",
                                parse_mode="Markdown")
    elif cmd == "adn":
        await chat.send_message("🧬 Componiendo el Wallet DNA… (~1 min)")
        from dna import wallet_dna_text
        text = await asyncio.to_thread(wallet_dna_text, arg)
        await chat.send_message(text or "Sin transacciones para esa dirección.",
                                parse_mode="Markdown")
    elif cmd == "prediccion":
        from influence import predict_text
        text = await asyncio.to_thread(predict_text, arg)
        await chat.send_message(text, parse_mode="Markdown")
    elif cmd == "similar":
        from similarity import similar_text
        text = await asyncio.to_thread(similar_text, arg)
        await chat.send_message(text, parse_mode="Markdown")
    elif cmd == "token":
        await chat.send_message("🧬 Analizando el token…")
        from token_report import token_report
        try:
            rep = await asyncio.to_thread(token_report, arg)
        except Exception as e:
            print(f"· token_report falló: {e}")
            await chat.send_message(
                "No pude analizar ese token ahora (la fuente de datos falló). "
                "Inténtalo de nuevo en un momento.")
            return
        if rep.get("found"):
            kb = _token_keyboard(rep.get("url"), arg)
            await _send_md(chat, rep["text"], reply_markup=kb,
                           disable_web_page_preview=True)
        else:
            await chat.send_message(
                "No encontré datos de token para esa dirección. "
                "Si es una billetera usa /ficha <address>.")
    elif cmd == "entidad":
        from entity_resolution import format_entity
        text = await asyncio.to_thread(format_entity, arg)
        await chat.send_message(text or "No detecto que esa wallet forme "
                                "parte de una entidad multi-wallet.",
                                parse_mode="Markdown")
    elif cmd == "ia":
        await chat.send_message("🧠 Perfilando y consultando a la IA… (~1 min)")
        text = await asyncio.to_thread(_ia_text, arg)
        await chat.send_message(text, parse_mode="Markdown")
    elif cmd == "evidencia":
        text = await asyncio.to_thread(_evidencia_text, arg)
        await chat.send_message(text, parse_mode="Markdown")
    elif cmd == "descartar":
        msg = await asyncio.to_thread(discard_wallet, arg)
        await chat.send_message(msg)
    elif cmd == "rastrear":
        msg = await asyncio.to_thread(restore_wallet, arg)
        await chat.send_message(msg)
    elif cmd == "preguntar":
        await chat.send_message("🤔 Consultando la base…")
        from ai_chat import answer_question
        resp = await asyncio.to_thread(answer_question, arg)
        await chat.send_message(resp)


# ─────────────────────────── JOBS PERIÓDICOS ──────────────────────────

async def backup_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import send_db_backup
        await asyncio.to_thread(send_db_backup)
    except Exception as e:
        print(f"· backup_job falló: {e}")


async def watchdog_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import watchdog_check, purgar_historial_bots
        await asyncio.to_thread(purgar_historial_bots)
        await asyncio.to_thread(watchdog_check)
    except Exception as e:
        print(f"· watchdog_job falló: {e}")


async def salud_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 6 h revisa la salud del sistema. Solo avisa si algo crítico
    falla (anti-ruido: no repite el mismo aviso en 12 h)."""
    try:
        from salud import revisar_y_avisar
        await asyncio.to_thread(revisar_y_avisar)
    except Exception as e:
        print(f"· salud_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "salud_job", e)
        except Exception:
            pass


async def performance_review_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 24h: el rendimiento MEDIDO decide quién conserva la ⭐.
    Degrada a las billeteras cuyas señales resultaron perdedoras."""
    try:
        from performance_review import review_tracked
        await asyncio.to_thread(review_tracked)
    except Exception as e:
        print(f"· performance_review_job falló: {e}")


async def learning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import weekly_learning
        await asyncio.to_thread(weekly_learning)
    except Exception as e:
        print(f"· learning_job falló: {e}")
    # Aprendizaje de qué tokens valen la pena (independiente del de señales)
    try:
        from token_learning import analyze_submitted
        await asyncio.to_thread(analyze_submitted)
    except Exception as e:
        print(f"· aprendizaje de tokens falló: {e}")


async def track_outcomes_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: mide el resultado (1h/24h) de las señales."""
    try:
        from signal_tracker import track_outcomes
        await asyncio.to_thread(track_outcomes)
    except Exception as e:
        print(f"· track_outcomes falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "track_outcomes", e)
        except Exception:
            pass


async def predictions_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Evalúa predicciones vencidas y rellena rendimiento del token.
    Fuera del webhook: aquí sí puede hacer llamadas de red sin bloquear."""
    try:
        from predictions import run_maintenance
        await asyncio.to_thread(run_maintenance)
    except Exception as e:
        print(f"· predictions_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "predictions_job", e)
        except Exception:
            pass


async def hypotheses_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Descubrimiento autónomo: la IA propone hipótesis cada 12 h."""
    try:
        from hypotheses import generate_hypotheses
        await asyncio.to_thread(generate_hypotheses)
    except Exception as e:
        print(f"· hypotheses_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "hypotheses_job", e)
        except Exception:
            pass


async def paper_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: revisa TP/SL/tiempo de las posiciones simuladas."""
    try:
        from paper_trading import update_open_trades
        await asyncio.to_thread(update_open_trades)
    except Exception as e:
        print(f"· paper_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "paper_job", e)
        except Exception:
            pass


def _resumen_diario_text() -> str:
    import time as _t
    conn = get_conn()
    ahora = int(_t.time())
    n24 = conn.execute(
        "SELECT COUNT(*) c FROM signals WHERE ts>=? AND side='compra'",
        (ahora - 86400,)).fetchone()["c"]
    med = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN chg_24h>0 THEN 1 ELSE 0 END) w "
        "FROM signals WHERE ts>=? AND side='compra' "
        "AND chg_24h IS NOT NULL", (ahora - 7 * 86400,)).fetchone()
    stars = conn.execute(
        "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
    conn.close()
    out = ["☀️ *Resumen diario*\n",
           f"Señales de compra (24h): {n24}",
           f"⭐ rastreadas: {stars}"]
    if med["c"]:
        wr = 100.0 * (med["w"] or 0) / med["c"]
        out.append(f"Win rate 7 días: {wr:.0f}% ({med['c']} medidas)")
    out.append("\nDetalle: /rendimiento · Simulación: /backtest")
    return "\n".join(out)


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID:
        return
    try:
        try:
            from digest import resumen_text
            txt = await asyncio.to_thread(resumen_text)
        except Exception:
            txt = await asyncio.to_thread(_resumen_diario_text)
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt,
                                   parse_mode="Markdown")
    except Exception as e:
        print(f"· resumen diario falló: {e}")


async def post_mortem_job(ctx: ContextTypes.DEFAULT_TYPE):
    """(Ola 11) Semanal: la IA revisa sus decisiones vs lo medido."""
    if not ADMIN_ID:
        return
    try:
        from post_mortem import generar
        txt = await asyncio.to_thread(generar)
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt,
                                   parse_mode="Markdown")
    except Exception as e:
        print(f"· post-mortem semanal falló: {e}")


async def auto_cycle_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: corre el ciclo y avisa al admin."""
    resumen = await asyncio.to_thread(run_full_cycle)
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🤖 Ciclo automático:\n\n{resumen}\n\nUsa /top para ver billeteras.")
        except Exception as e:
            print(f"No se pudo avisar al admin: {e}")


async def sync_webhook_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Re-sincroniza el webhook de Helius con las billeteras ⭐ actuales, por
    si alguna se volvió rastreada entre ciclos (así ninguna se queda sin
    monitorear hasta el próximo ciclo de 6h)."""
    try:
        msg = await asyncio.to_thread(sync_helius_webhook)
        print(f"📡 Re-sync webhook: {msg}")
    except Exception as e:
        print(f"· sync_webhook_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "sync_webhook_job", e)
        except Exception:
            pass


# ─────────────────────────── SEGURIDAD ────────────────────────────────

def solo_admin(func):
    """Decorador: ignora mensajes de cualquiera que no sea el dueño."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if ADMIN_ID and update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ No autorizado.")
            return
        return await func(update, ctx)
    return wrapper


# ─────────────────────────── CALLBACKS ────────────────────────────────

async def _hub_run(q, name: str):
    """Ejecuta una acción de botón del hub (h:run:<name>)."""
    chat = q.message.chat
    if name == "status":
        await q.answer()
        await q.edit_message_text(_status_text(), parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name in ("top10", "top20"):
        limit = 10 if name == "top10" else 20
        await q.answer()
        text, kb = await asyncio.to_thread(build_top_message, limit)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    elif name == "rendimiento":
        await q.answer()
        from rendimiento import rendimiento_text
        txt = await asyncio.to_thread(rendimiento_text)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "backtest":
        await q.answer()
        from rendimiento import backtest_text
        txt = await asyncio.to_thread(backtest_text, 0.5)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "saldos":
        await q.answer("💰 Consultando saldos…")
        txt = await asyncio.to_thread(_saldos_text)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "hermanas":
        await q.answer("🔗 Buscando vínculos… (~1 min)")
        from wallet_links import find_links
        txt = await asyncio.to_thread(find_links)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "senales":
        await q.answer()
        await q.edit_message_text(_senales_text(), parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "ciclo":
        await q.answer("⏳ Iniciando ciclo…")
        await chat.send_message("⏳ Ciclo completo iniciado…")
        resumen = await asyncio.to_thread(run_full_cycle)
        await chat.send_message(resumen)
    elif name == "descubrir":
        await q.answer("⏳ Buscando…")
        await chat.send_message("⏳ Buscando tokens ganadores…")
        saved = await asyncio.to_thread(run_discovery)
        await chat.send_message(f"✅ {saved} tokens ganadores guardados.")
    elif name == "analizar":
        await q.answer("⏳ Analizando…")
        await chat.send_message("⏳ Analizando compradores tempranos…")
        await asyncio.to_thread(run_analysis)
        await chat.send_message("✅ Análisis terminado. Usa /top.")


async def handle_hub(q, ctx: ContextTypes.DEFAULT_TYPE):
    """Router de navegación del hub (callback_data que empieza con 'h:')."""
    action = (q.data or "")[2:]
    if action == "home":
        await q.answer()
        await q.edit_message_text(hub_text(), parse_mode="Markdown",
                                  reply_markup=kb_home())
    elif action == "help":
        await q.answer()
        await q.edit_message_text(HELP_TEXT, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif action in SECCIONES:
        texto, kb = SECCIONES[action]
        await q.answer()
        await q.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb())
    elif action == "app":
        kb = app_keyboard()
        if not kb:
            await q.answer("Falta PUBLIC_URL para el panel.", show_alert=True)
            return
        await q.answer()
        await q.message.chat.send_message(
            "Tu panel visual — top de billeteras, señales y stats en vivo:",
            reply_markup=kb)
    elif action.startswith("run:"):
        await _hub_run(q, action[4:])
    elif action.startswith("ask:"):
        cmd = action[4:]
        AWAITING[q.from_user.id] = cmd
        await q.answer()
        await q.edit_message_text(
            ASK_PROMPTS.get(cmd, "Envíame el dato:"),
            parse_mode="Markdown", reply_markup=kb_cancelar())
    else:
        await q.answer()


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los botones inline: hub, /top y confirmaciones del agente."""
    q = update.callback_query
    if ADMIN_ID and q.from_user.id != ADMIN_ID:
        await q.answer("⛔ No autorizado", show_alert=True)
        return
    data = q.data or ""

    # Navegación del hub
    if data.startswith("h:"):
        # si el usuario estaba en un "envíame la dirección", lo cancelamos
        if not data.startswith("h:ask:"):
            AWAITING.pop(q.from_user.id, None)
        await handle_hub(q, ctx)
        return

    # Paper trading: ver resumen, pedir confirmación y reiniciar.
    # Va en dos pasos a propósito — 'pap:pedir' solo pregunta, y solo
    # 'pap:reset' borra. Un dedo mal puesto no debe costar el historial.
    if data.startswith("pap:"):
        accion = data[4:]
        from paper_trading import contar, resumen_text, reset
        if accion == "ver":
            await q.answer()
            txt = await asyncio.to_thread(resumen_text)
            try:
                await q.edit_message_text(txt, parse_mode="Markdown",
                                          reply_markup=kb_paper())
            except Exception:
                # Telegram rechaza editar si el texto no cambió
                await q.message.chat.send_message(
                    txt, parse_mode="Markdown", reply_markup=kb_paper())
            return
        if accion == "pedir":
            abiertas, cerradas = await asyncio.to_thread(contar)
            if abiertas + cerradas == 0:
                await q.answer("No hay nada que reiniciar.", show_alert=True)
                return
            await q.answer()
            await q.edit_message_text(
                f"🗑 *Reiniciar paper trading*\n\n"
                f"Se borrarán *{abiertas}* posiciones abiertas y "
                f"*{cerradas}* cerradas.\n"
                f"⚠️ No se puede deshacer.",
                parse_mode="Markdown",
                reply_markup=kb_paper_confirmar(abiertas, cerradas))
            return
        if accion == "reset":
            await q.answer("⏳ Borrando…")
            try:
                abiertas, cerradas = await asyncio.to_thread(reset)
            except Exception as e:
                await q.edit_message_text(f"❌ No se pudo reiniciar: {e}")
                return
            txt = await asyncio.to_thread(resumen_text)
            await q.edit_message_text(
                f"✅ *Paper trading reiniciado*\n"
                f"Borradas {abiertas} abiertas y {cerradas} cerradas.\n\n"
                + txt, parse_mode="Markdown", reply_markup=kb_paper())
            return
        await q.answer()
        return

    # Feedback 👍/👎 sobre un token enviado (aprendizaje)
    if data.startswith("tk:up:") or data.startswith("tk:dn:"):
        good = data.startswith("tk:up:")
        mint = data.split(":", 2)[2]
        try:
            from token_learning import set_feedback
            await asyncio.to_thread(set_feedback, mint, good)
        except Exception:
            pass
        await q.answer("👍 ¡Gracias! Lo tendré en cuenta."
                       if good else "👎 Anotado, aprenderé de esto.")
        return

    # Botones bajo las alertas de señal
    if data.startswith("ficha:") or data.startswith("saldo1:") \
            or data.startswith("adel:"):
        addr = data.split(":", 1)[1]
        if data.startswith("adel:"):
            msg = await asyncio.to_thread(discard_wallet, addr)
            await q.answer((msg or "Hecho")[:190])
            return
        await q.answer("⏳ Consultando…")
        if data.startswith("ficha:"):
            txt = await asyncio.to_thread(_ficha_text, addr)
            txt = txt or "Sin datos para esa dirección."
        else:
            txt = await asyncio.to_thread(_saldo_uno_text, addr)
        await q.message.chat.send_message(txt, parse_mode="Markdown")
        return

    # Confirmación de acciones del agente IA
    if data == "agc:y" or data == "agc:n":
        accion = PENDING_ACTIONS.pop(q.from_user.id, None)
        if data == "agc:n" or not accion:
            await q.answer("Cancelado")
            try:
                await q.edit_message_text("❌ Acción cancelada.")
            except Exception:
                pass
            return
        await q.answer("Ejecutando…")
        from ai_agent import execute_action
        resultado = await asyncio.to_thread(execute_action, accion)
        try:
            await q.edit_message_text(f"✅ {resultado}")
        except Exception:
            pass
        return

    # Botones del /top: descartar (d:) o cambiar tamaño (t:)
    if data.startswith("d:"):
        try:
            _, limit, address = data.split(":", 2)
            limit = int(limit)
        except ValueError:
            await q.answer("Dato inválido")
            return
        msg = await asyncio.to_thread(discard_wallet, address)
        await q.answer(msg[:190])
        text, kb = await asyncio.to_thread(build_top_message, limit)
        try:
            await q.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=kb)
        except Exception:
            pass  # el mensaje no cambió o expiró
    elif data.startswith("t:"):
        try:
            limit = int(data[2:])
        except ValueError:
            await q.answer()
            return
        await q.answer()
        text, kb = await asyncio.to_thread(build_top_message, limit)
        try:
            await q.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=kb)
        except Exception:
            pass


# ─────────────────────────── COMANDOS ─────────────────────────────────

@solo_admin
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AWAITING.pop(update.effective_user.id, None)
    await update.message.reply_text(hub_text(), parse_mode="Markdown",
                                    reply_markup=kb_home())


@solo_admin
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@solo_admin
async def cmd_descubrir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Buscando tokens ganadores…")
    saved = await asyncio.to_thread(run_discovery)
    await update.message.reply_text(f"✅ {saved} tokens ganadores guardados.")


@solo_admin
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando compradores tempranos…")
    await asyncio.to_thread(run_analysis)
    await update.message.reply_text("✅ Análisis terminado. Usa /top.")


@solo_admin
async def cmd_ciclo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Ciclo completo iniciado…")
    resumen = await asyncio.to_thread(run_full_cycle)
    await update.message.reply_text(resumen)


@solo_admin
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    limit = 10
    if ctx.args:
        try:
            limit = max(5, min(30, int(ctx.args[0])))
        except ValueError:
            pass
    text, kb = await asyncio.to_thread(build_top_message, limit)
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=kb)


@solo_admin
async def cmd_descartar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /descartar <address>")
        return
    await run_address_command(update.message.chat, "descartar", ctx.args[0])


@solo_admin
async def cmd_rastrear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /rastrear <address>")
        return
    await run_address_command(update.message.chat, "rastrear", ctx.args[0])


@solo_admin
async def cmd_evidencia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /evidencia <address>")
        return
    await run_address_command(update.message.chat, "evidencia", ctx.args[0])


@solo_admin
async def cmd_perfil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /perfil <address>")
        return
    await run_address_command(update.message.chat, "perfil", ctx.args[0])


@solo_admin
async def cmd_ia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ia <address>")
        return
    await run_address_command(update.message.chat, "ia", ctx.args[0])


@solo_admin
async def cmd_ficha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ficha <address>")
        return
    await run_address_command(update.message.chat, "ficha", ctx.args[0])


@solo_admin
async def cmd_preguntar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: /preguntar <pregunta>\nEj: /preguntar ¿qué billetera "
            "tuvo mejor resultado en sus señales?")
        return
    await run_address_command(update.message.chat, "preguntar",
                              " ".join(ctx.args))


def _alias_md(s) -> str:
    """Alias seguro para Markdown (Ola 6 - M21): un apodo con _, ` o [
    rompia el parseo y Telegram rechazaba el MENSAJE ENTERO (/elite,
    /saldos y el digest de las 13:00 fallaban en silencio)."""
    return (str(s or "").replace("*", "").replace("_", " ")
            .replace("`", "").replace("[", "(").replace("]", ")"))


@solo_admin
async def cmd_senales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # to_thread (Ola 6 - M25): las consultas corrian EN el event loop;
    # un stall de SQLite (busy_timeout 30 s) congelaba el bot entero.
    texto = await asyncio.to_thread(_senales_text)
    await update.message.reply_text(texto, parse_mode="Markdown")


@solo_admin
async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Abre el panel visual (Mini App de Telegram)."""
    kb = app_keyboard()
    if not kb:
        await update.message.reply_text("Falta PUBLIC_URL para el panel.")
        return
    await update.message.reply_text(
        "Tu panel visual — top de billeteras, señales y stats en vivo:",
        reply_markup=kb)


@solo_admin
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = await asyncio.to_thread(_status_text)
    await update.message.reply_text(texto, parse_mode="Markdown")


@solo_admin
async def cmd_top_alertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cuántas billeteras del ranking pueden alertar. /topalertas [n]

    0 = sin límite (vuelve al comportamiento de antes: alerta cualquier ⭐).
    """
    from db import set_setting, get_setting, TOP_ALERTAS_DEFAULT
    conn = get_conn()
    try:
        args = ctx.args or []
        if args:
            try:
                n = int(float(args[0]))
                if n < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "Uso: /topalertas <n>   (0 = sin límite)")
                return
            set_setting(conn, "top_alertas", n)
            txt = (f"📡 Alertan las *top {n}* billeteras."
                   if n else "📡 Sin límite: alerta cualquier billetera ⭐.")
            await update.message.reply_text(txt, parse_mode="Markdown")
            return
        actual = get_setting(conn, "top_alertas", str(TOP_ALERTAS_DEFAULT))
        # (Ola 8) 0 significa "sin limite" para todo el sistema: decir
        # "top 0" seria mentira.
        _desc = (f"las *top {actual}* billeteras"
                 if int(float(actual or 0)) else
                 "*todas* las billeteras ⭐ (sin límite)")
        await update.message.reply_text(
            f"📡 Ahora mismo alertan {_desc} "
            f"(señales y tarjetas).\nCambiar: `/topalertas 20`  ·  "
            f"`/topalertas 0` quita el límite.",
            parse_mode="Markdown")
    finally:
        conn.close()


@solo_admin
async def cmd_paper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paper trading: /paper (resumen) · /paper on|off · /paper max <SOL>
    · /paper reset (con confirmación)."""
    from db import set_setting
    from paper_trading import resumen_text
    args = [a.lower() for a in (ctx.args or [])]
    if args:
        if args[0] == "reset":
            # No borra aquí: enseña lo que se perdería y pide confirmación.
            from paper_trading import contar
            abiertas, cerradas = await asyncio.to_thread(contar)
            if abiertas + cerradas == 0:
                await update.message.reply_text(
                    "🧪 No hay nada que reiniciar: el historial ya está vacío.")
                return
            await update.message.reply_text(
                f"🗑 *Reiniciar paper trading*\n\n"
                f"Se borrarán *{abiertas}* posiciones abiertas y "
                f"*{cerradas}* cerradas.\n"
                f"⚠️ No se puede deshacer.",
                parse_mode="Markdown",
                reply_markup=kb_paper_confirmar(abiertas, cerradas))
            return
        conn = get_conn()
        if args[0] in ("on", "off"):
            set_setting(conn, "paper_enabled", "1" if args[0] == "on" else "0")
            estado = "activado 🟢" if args[0] == "on" else "apagado 🔴"
            conn.close()
            await update.message.reply_text(f"🧪 Paper trading {estado}.")
            return
        if args[0] == "max" and len(args) > 1:
            try:
                v = float(args[1])
                if v <= 0:
                    raise ValueError
            except ValueError:
                conn.close()
                await update.message.reply_text(
                    "Uso: /paper max <SOL>  (ej: /paper max 1.5)")
                return
            set_setting(conn, "paper_max_sol", v)
            conn.close()
            await update.message.reply_text(
                f"🧪 Tope por señal: *{v:g} SOL*", parse_mode="Markdown")
            return
        conn.close()
        await update.message.reply_text(
            "Uso: /paper · /paper on · /paper off · /paper max <SOL> "
            "· /paper reset")
        return
    txt = await asyncio.to_thread(resumen_text)
    await update.message.reply_text(txt, parse_mode="Markdown",
                                    reply_markup=kb_paper())


# SIN @solo_admin (Ola 6, auditoria 19/8 - C5): esto es un helper de
# fondo, no un handler. El decorador lo envolvia como wrapper(update,
# ctx) de 2 parametros; la llamada le pasa 4 → TypeError en CADA mint
# pegado que generaba ficha, y la extraccion de compradores (la que
# alimenta la red de billeteras) no corrio NUNCA — disfrazada del
# "⚠️ Algo falló" generico. La seguridad ya la puso on_chat, que si
# es handler y si esta decorado.
async def _extract_buyers_bg(chat, mint: str, symbol, chg24):
    """En segundo plano: extrae los compradores del token enviado y los mete
    a la red (mismo análisis que un token ganador). No bloquea la ficha."""
    try:
        from token_extract import extract_buyers
        status, n = await asyncio.to_thread(
            extract_buyers, mint, symbol, chg24)
        nombre = f"${symbol}" if symbol else mint[:6]
        if status == "ok":
            msg = (f"🧠 Analicé los compradores de {nombre}: "
                   f"{n} billeteras registradas en tu red. "
                   f"Entrarán al grading en el próximo ciclo.")
            # ¿El dev vendió? (solo cuando la extracción corrió → hereda límite)
            try:
                from dev_check import dev_line
                dl = await asyncio.to_thread(dev_line, mint)
                if dl:
                    msg += "\n" + dl
            except Exception as e:
                print(f"· dev_line en bg falló: {e}")
            await chat.send_message(msg)
        elif status == "rate":
            await chat.send_message(
                "⏳ Alcancé el límite de análisis de tokens por hora "
                "(para cuidar la cuota). Intenta de nuevo más tarde.")
        # "cache" y "error" no molestan al usuario
    except Exception as e:
        print(f"· _extract_buyers_bg falló: {e}")


async def on_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chat libre. Si el hub estaba esperando un dato, lo consume aquí;
    si no, cualquier texto sin /comando activa al agente IA."""
    texto = (update.message.text or "").strip()
    if not texto:
        return

    # ¿El usuario había pulsado un botón que pedía dirección/pregunta?
    cmd = AWAITING.pop(update.effective_user.id, None)
    if cmd:
        await run_address_command(update.message.chat, cmd, texto)
        return

    # ¿Es un contrato/mint pegado directo? → ficha de token
    if _MINT_RE.match(texto):
        await update.message.chat.send_action("typing")
        from token_report import token_report
        try:
            rep = await asyncio.to_thread(token_report, texto)
        except Exception as e:
            print(f"· token_report falló: {e}")
            await update.message.reply_text(
                "No pude analizar ese token ahora (la fuente de datos falló). "
                "Inténtalo de nuevo en un momento.")
            return
        if rep.get("found"):
            kb = _token_keyboard(rep.get("url"), texto)
            await _send_md(update.message.chat, rep["text"],
                           reply_markup=kb, disable_web_page_preview=True)
            # En segundo plano: extraer compradores → alimenta la red.
            # Se guarda la referencia: sin ella el GC puede matar la tarea.
            data = rep.get("data") or {}
            tarea = asyncio.create_task(_extract_buyers_bg(
                update.message.chat, texto,
                data.get("symbol"), data.get("price_change_h24")))
            _BG_TASKS.add(tarea)
            tarea.add_done_callback(_BG_TASKS.discard)
            return
        # no es un token tradeable (¿billetera?) → sigue el flujo normal

    await update.message.chat.send_action("typing")
    from ai_agent import chat, describe_action
    respuesta, accion = await asyncio.to_thread(chat, texto)
    if accion:
        PENDING_ACTIONS[update.effective_user.id] = accion
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirmar", callback_data="agc:y"),
            InlineKeyboardButton("❌ Cancelar", callback_data="agc:n")]])
        msg = (respuesta + "\n\n" if respuesta else "") + \
            f"¿Ejecuto esta acción?\n{describe_action(accion)}"
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=kb)
    else:
        await update.message.reply_text(respuesta)


# ─────────────────────────── ARRANQUE ─────────────────────────────────

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador global: registra cualquier excepción no capturada de un
    comando. Antes se perdía en silencio y el usuario solo veía que el bot
    no respondía."""
    err = getattr(ctx, "error", None)
    print(f"· Error no capturado: {err}")
    try:
        from errores import record
        await asyncio.to_thread(record, "telegram", err, "handler")
    except Exception:
        pass
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Algo falló al procesar eso. Ya quedó registrado; "
                "míralo con /errores.")
    except Exception:
        pass


async def _post_init(app: Application):
    """Registra el menú de comandos que se ve al pulsar '/' en Telegram."""
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Abrir el menú principal"),
            BotCommand("resumen", "Resumen del día (todo en una vista)"),
            BotCommand("menu", "Abrir el menú principal"),
            BotCommand("top", "Top de billeteras"),
            BotCommand("senales", "Últimas señales"),
            BotCommand("status", "Estado del sistema"),
            BotCommand("app", "Panel visual (Mini App)"),
            BotCommand("ciclo", "Correr el pipeline ahora"),
            BotCommand("preguntar", "Preguntar a la IA sobre tu base"),
            BotCommand("rendimiento", "Win rate de las señales"),
            BotCommand("estrellasperf", "Rendimiento medido de cada ⭐"),
            BotCommand("salud", "¿Está todo funcionando bien?"),
            BotCommand("datos", "Conocimiento propio acumulado"),
            BotCommand("reevaluar", "Volver a graduar las billeteras"),
            BotCommand("exportar", "Descargar todo en JSON (para IA local)"),
            BotCommand("errores", "Errores registrados (24 h)"),
            BotCommand("backtest", "Simular copiar las señales"),
            BotCommand("paper", "Paper trading simulado"),
            BotCommand("topalertas", "Cuántas billeteras pueden alertar"),
            BotCommand("saldos", "Saldo SOL de las vigiladas"),
            BotCommand("hermanas", "Billeteras del mismo dueño"),
            BotCommand("ficha", "Ficha completa de una billetera"),
            BotCommand("adn", "Wallet DNA completo de una billetera"),
            BotCommand("clusters", "Redes de co-compra detectadas"),
            BotCommand("prediccion", "Quién comprará tras una billetera"),
            BotCommand("lideres", "Líderes ocultos de la red"),
            BotCommand("predicciones", "Señales predictivas y su precisión"),
            BotCommand("metricas", "Panel de rendimiento del motor"),
            BotCommand("backup", "Descargar copia de la base de datos"),
            BotCommand("elite", "Clasificación Elite/Seguimiento/Observación"),
            BotCommand("alpha", "Quién descubre gemas antes que el mercado"),
            BotCommand("atencion", "Score de Atención (quién aporta info nueva)"),
            BotCommand("similar", "Billeteras parecidas a una dada"),
            BotCommand("estrellas", "Estrellas emergentes (clones de Elite)"),
            BotCommand("token", "Token DNA de un mint"),
            BotCommand("hipotesis", "Hipótesis autónomas del sistema"),
            BotCommand("entidad", "Wallets del mismo operador (evidencia)"),
        ])
    except Exception as e:
        print(f"· set_my_commands falló: {e}")


def _marcar_reevaluacion(solo_sin_estrella: bool = True) -> int:
    """Marca billeteras para que el próximo ciclo las vuelva a graduar.

    Se limpia `pnl_updated`, que es lo que el selector usa para saber si un
    veredicto caducó. Útil tras cambiar los criterios de grading: sin esto
    habría que esperar los días de caducidad para ver el efecto.
    """
    conn = get_conn()
    try:
        cond = ("WHERE COALESCE(is_bot,0)=0 AND ai_class IS NOT NULL"
                + (" AND COALESCE(is_tracked,0)=0" if solo_sin_estrella else ""))
        n = conn.execute(f"SELECT COUNT(*) c FROM wallets {cond}"
                         ).fetchone()["c"]
        conn.execute(f"UPDATE wallets SET pnl_updated=NULL {cond}")
        conn.commit()
        return n
    finally:
        conn.close()


@solo_admin
async def cmd_reevaluar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Vuelve a graduar las billeteras ya evaluadas (tras cambiar criterios)."""
    todas = bool(ctx.args and ctx.args[0].lower() in ("todas", "all"))
    n = await asyncio.to_thread(_marcar_reevaluacion, not todas)
    await update.message.reply_text(
        f"🔄 {n} billeteras marcadas para volver a graduarse.\n"
        f"Se irán procesando en los próximos ciclos "
        f"({config.MAX_EVAL_PER_CYCLE} por ciclo, cada "
        f"{AUTO_CYCLE_HOURS:g} h).\n\n"
        "Usa /ciclo si quieres empezar ya.")


@solo_admin
async def cmd_datos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qué conocimiento propio llevamos acumulado (sin depender de Helius)."""
    from exportar import resumen
    from trades_store import resumen_text
    txt = await asyncio.to_thread(resumen)
    txt2 = await asyncio.to_thread(resumen_text)
    await _send_md(update.message.chat, txt + "\n\n" + txt2)


@solo_admin
async def cmd_exportar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Exporta TODO el conocimiento a JSON y lo envía como archivo.
    Pensado para analizarlo fuera del bot (p. ej. con una IA local)."""
    await update.message.reply_text("📦 Preparando el export…")
    from exportar import exportar
    rutas = await asyncio.to_thread(exportar)
    if isinstance(rutas, str):        # compatibilidad por si vuelve una sola
        rutas = [rutas]
    if not rutas:
        await update.message.reply_text("No se pudo generar el export.")
        return

    import os as _os
    total = len(rutas)
    if total > 1:
        await update.message.reply_text(
            f"El export ocupa demasiado para un solo archivo, van *{total} "
            "partes*. Descárgalas todas: cada una es un JSON comprimido "
            "válido por sí mismo.", parse_mode="Markdown")

    enviadas = 0
    for i, ruta in enumerate(rutas, start=1):
        try:
            mb = _os.path.getsize(ruta) / 1e6
            cap = (f"📦 Conocimiento acumulado ({mb:.1f} MB)"
                   if total == 1 else f"📦 Parte {i}/{total} ({mb:.1f} MB)")
            with open(ruta, "rb") as fh:
                await update.message.reply_document(
                    document=fh, filename=_os.path.basename(ruta),
                    caption=cap)
            enviadas += 1
        except Exception as e:
            await update.message.reply_text(
                f"No se pudo enviar la parte {i}/{total}: {e}")
        finally:
            try:
                _os.remove(ruta)      # no dejar GB muertos en el disco
            except OSError:
                pass
    if enviadas:
        await update.message.reply_text(
            f"✅ Export completo: {enviadas}/{total} archivo(s).")


@solo_admin
async def cmd_salud(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Autodiagnóstico. '/salud ia' añade la lectura interpretada."""
    con_ia = bool(ctx.args and ctx.args[0].lower() in ("ia", "ai"))
    await update.message.chat.send_action("typing")
    from salud import salud_text
    txt = await asyncio.to_thread(salud_text, None, con_ia)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_errores(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Errores registrados en las últimas 24 h (o las que se indiquen)."""
    horas = 24
    if ctx.args:
        try:
            horas = max(1, min(168, int(ctx.args[0])))
        except ValueError:
            pass
    from errores import errores_text
    txt = await asyncio.to_thread(errores_text, horas)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_wallets_perf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rendimiento MEDIDO de cada ⭐ (no degrada nada, solo informa)."""
    from performance_review import review_text
    txt = await asyncio.to_thread(review_text)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_rendimiento(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from rendimiento import rendimiento_text
    txt = await asyncio.to_thread(rendimiento_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    monto = 0.5
    if ctx.args:
        try:
            monto = max(0.05, min(50.0, float(ctx.args[0])))
        except ValueError:
            pass
    from rendimiento import backtest_text
    txt = await asyncio.to_thread(backtest_text, monto)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_adn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /adn <address>")
        return
    await run_address_command(update.message.chat, "adn", ctx.args[0])


@solo_admin
async def cmd_clusters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕸 Buscando clusters de co-compra…")
    from clusters import clusters_text
    txt = await asyncio.to_thread(clusters_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_prediccion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /prediccion <address>")
        return
    await run_address_command(update.message.chat, "prediccion", ctx.args[0])


@solo_admin
async def cmd_lideres(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕵️ Buscando líderes ocultos…")
    from influence import hidden_leaders_text
    txt = await asyncio.to_thread(hidden_leaders_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_predicciones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from predictions import predictions_text
    txt = await asyncio.to_thread(predictions_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_metricas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from predictions import metrics_text
    txt = await asyncio.to_thread(metrics_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_elite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = await asyncio.to_thread(_elite_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_alpha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Midiendo quién descubre antes…")
    from alpha import alpha_text
    txt = await asyncio.to_thread(alpha_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_atencion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Calculando Score de Atención…")
    from attention import attention_text
    txt = await asyncio.to_thread(attention_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Componiendo el resumen…")
    from digest import resumen_text
    txt = await asyncio.to_thread(resumen_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_similar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /similar <address>")
        return
    await run_address_command(update.message.chat, "similar", ctx.args[0])


@solo_admin
async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /token <mint>")
        return
    await run_address_command(update.message.chat, "token", ctx.args[0])


@solo_admin
async def cmd_estrellas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌱 Buscando estrellas emergentes…")
    from similarity import rising_stars_text
    txt = await asyncio.to_thread(rising_stars_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_hipotesis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Generando hipótesis del sistema…")
    from hypotheses import hypotheses_text
    txt = await asyncio.to_thread(hypotheses_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_entidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await run_address_command(update.message.chat, "entidad", ctx.args[0])
        return
    from entity_resolution import entities_text
    txt = await asyncio.to_thread(entities_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _elite_text() -> str:
    conn = get_conn()
    # (Ola 8, 21/8) El corte a 40 era silencioso: con 280 graduadas en la
    # base, la cola desaparecia y la lista parecia completa. Ahora se dice.
    graduadas = conn.execute(
        """SELECT COUNT(*) c FROM wallets
           WHERE grade IN ('Elite','Seguimiento','Observación')
             AND is_bot=0""").fetchone()["c"]
    rows = conn.execute(
        """SELECT address, alias, grade, consistency, pnl_total, wallet_score
           FROM wallets
           WHERE grade IN ('Elite','Seguimiento','Observación') AND is_bot=0
           ORDER BY CASE grade WHEN 'Elite' THEN 0 WHEN 'Seguimiento' THEN 1
                    ELSE 2 END,
                    COALESCE(wallet_score,0) DESC,
                    COALESCE(pnl_total,-1e9) DESC
           LIMIT 40""").fetchall()
    conn.close()
    if not rows:
        return ("Aún no hay billeteras clasificadas. Se irán graduando cuando "
                "la IA re-evalúe cada billetera (cada ~3 días o con /analizar).")
    emo = {"Elite": "⭐", "Seguimiento": "🟢", "Observación": "🟡"}
    titulo = ("🏆 *Clasificación de billeteras*"
              + (f"  _(top {len(rows)} de {graduadas})_"
                 if graduadas > len(rows) else "") + "\n")
    out, actual = [titulo], None
    for r in rows:
        if r["grade"] != actual:
            actual = r["grade"]
            out.append(f"\n{emo.get(actual,'')} *{actual}*")
        nombre = _alias_md(r["alias"] or r["address"][:8])
        cons = f" · C{round(r['consistency'])}" if r["consistency"] is not None else ""
        pnl = f" · {r['pnl_total']:+.0f} SOL" if r["pnl_total"] is not None else ""
        sc = f" · score {round(r['wallet_score'])}" if r["wallet_score"] is not None else ""
        out.append(f"• {nombre}{sc}{pnl}{cons}")
    out.append("\n_C = Consistency Score. Elite = rentable, consistente y "
               "líder. PnL y score son de la última evaluación de cada "
               "billetera (se refrescan cada 3-14 días). "
               "Usa /adn <address> para el detalle._")
    return "\n".join(out)


@solo_admin
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💾 Preparando copia de la base de datos…")
    try:
        from backup import make_backup
        path, fname, caption = await asyncio.to_thread(make_backup)
        with open(path, "rb") as fh:
            await update.message.reply_document(document=fh, filename=fname,
                                                caption=caption)
        try:
            os.remove(path)
        except OSError:
            pass
    except Exception as e:
        await update.message.reply_text(f"No pude generar el backup: {e}")


@solo_admin
async def cmd_ialocal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """IA local para salidas: /ialocal <url> · /ialocal on|off · /ialocal"""
    from db import set_setting, get_setting
    conn = get_conn()
    try:
        args = ctx.args or []
        if args:
            a = args[0].strip()
            if a.lower() == "off":
                set_setting(conn, "ia_local_activa", 0)
                await update.message.reply_text(
                    "🤖 IA local APAGADA: todo vuelve a reglas.")
                return
            if a.lower() == "proveedor":
                if len(args) > 1 and args[1].lower() in (
                        "local", "nube", "local_primero"):
                    set_setting(conn, "ia_proveedor", args[1].lower())
                    await update.message.reply_text(
                        f"🧠 Proveedor de IA: *{args[1].lower()}*",
                        parse_mode="Markdown")
                else:
                    actual = get_setting(conn, "ia_proveedor",
                                         "local_primero")
                    await update.message.reply_text(
                        f"🧠 Proveedor actual: {actual}\n"
                        "Cambiar: /ialocal proveedor local · "
                        "local_primero · nube")
                return
            if a.lower() == "on":
                url = get_setting(conn, "local_ai_url", "") or ""
                if not url:
                    await update.message.reply_text(
                        "Primero configura la URL: /ialocal https://…")
                    return
                set_setting(conn, "ia_local_activa", 1)
                await update.message.reply_text(
                    f"🤖 IA local ENCENDIDA · {url}\n"
                    "Mitad de las posiciones nuevas serán gestionadas "
                    "por la IA (A/B contra reglas).")
                return
            if a.startswith("http"):
                set_setting(conn, "local_ai_url", a.rstrip("/"))
                set_setting(conn, "ia_local_activa", 1)
                await update.message.reply_text(
                    f"🤖 URL guardada y experimento ENCENDIDO:\n{a}\n"
                    "Si el túnel cambia de URL, repite /ialocal <url>.")
                return
            await update.message.reply_text(
                "Uso: /ialocal <url> · /ialocal on · /ialocal off")
            return
        activa = get_setting(conn, "ia_local_activa", "0")
        url = get_setting(conn, "local_ai_url", "") or "(sin URL)"
        estado = "🟢 encendida" if str(activa) in ("1", "1.0") \
            else "🔴 apagada"
        await update.message.reply_text(
            f"🤖 IA local: {estado}\nURL: {url}\n"
            "Cambiar: /ialocal <url> · /ialocal on · /ialocal off")
    finally:
        conn.close()


@solo_admin
async def cmd_postmortem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/postmortem — decisiones vs resultado medido. 'ya' = regenerar."""
    fresco = bool(ctx.args and ctx.args[0].lower() in ("ya", "ahora"))
    if fresco:
        await update.message.reply_text(
            "⏳ Generando el post-mortem (la IA puede tardar un par de "
            "minutos)…")
    from post_mortem import post_mortem_text
    txt = await asyncio.to_thread(post_mortem_text, fresco)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_salidas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deriva post-venta por billetera: ¿vende temprano o sale en la cima?"""
    from salidas import salidas_text, hold_report
    conn = get_conn()
    try:
        txt = salidas_text(conn)
        extra = hold_report(conn)
        if extra:
            txt += "\n" + extra
    finally:
        conn.close()
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_hermanas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 Buscando vínculos entre billeteras ⭐… (puede tardar ~1 min)")
    from wallet_links import find_links
    txt = await asyncio.to_thread(find_links)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _saldo_uno_text(addr: str) -> str:
    import requests as _rq
    try:
        resp = _rq.post(config.HELIUS_RPC,
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": "getBalance", "params": [addr]},
                        timeout=15)
        sol = resp.json()["result"]["value"] / 1e9
        return f"💰 Saldo de `{addr[:10]}…`: *{sol:,.2f} SOL*"
    except Exception:
        return "No pude consultar el saldo."


@solo_admin
async def cmd_saldos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Consultando saldos on-chain…")
    txt = await asyncio.to_thread(_saldos_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _saldos_text():
    if True:
        import requests as _rq
        conn = get_conn()
        # (Ola 8, 21/8) Antes el encabezado decia "billeteras vigiladas" y
        # el pie "Total combinado" cuando la query cortaba a 25 de miles
        # (1.209 en la base el 21/8) y el total sumaba solo las consultadas
        # sin error. Ahora el corte y el alcance del total se dicen.
        poblacion = conn.execute(
            """SELECT COUNT(*) c FROM wallets
               WHERE is_tracked=1
                  OR (is_bot=0 AND winning_tokens_count >= 2)""").fetchone()["c"]
        rows = conn.execute(
            """SELECT address, alias, is_tracked FROM wallets
               WHERE is_tracked=1
                  OR (is_bot=0 AND winning_tokens_count >= 2)
               ORDER BY is_tracked DESC, score DESC LIMIT 25""").fetchall()
        conn.close()
        out = [f"💰 *Saldos — top {len(rows)} de {poblacion} vigiladas:*\n"]
        total = 0.0
        errores = 0
        for r in rows:
            try:
                resp = _rq.post(config.HELIUS_RPC,
                                json={"jsonrpc": "2.0", "id": 1,
                                      "method": "getBalance",
                                      "params": [r["address"]]},
                                timeout=15)
                sol = resp.json()["result"]["value"] / 1e9
            except Exception:
                sol = None
            nombre = _alias_md(r["alias"] or r["address"][:8])
            icono = "⭐" if r["is_tracked"] else "👁"
            if sol is None:
                errores += 1
                out.append(f"{icono} {nombre}: _error al consultar_")
            else:
                total += sol
                out.append(f"{icono} {nombre}: *{sol:,.2f} SOL*"
                           f"  `{r['address'][:8]}…`")
        consultadas = len(rows) - errores
        out.append(f"\nTotal de las {consultadas} consultadas: "
                   f"*{total:,.2f} SOL*"
                   + (f"  _({errores} con error)_" if errores else ""))

    return "\n".join(out)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Créalo con @BotFather.")
    if not ADMIN_ID:
        print("⚠️  TELEGRAM_ADMIN_ID no configurado: el bot responderá a "
              "CUALQUIERA. Configúralo antes de usarlo en serio.")
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ciclo", cmd_ciclo))
    app.add_handler(CommandHandler("descubrir", cmd_descubrir))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("descartar", cmd_descartar))
    app.add_handler(CommandHandler("rastrear", cmd_rastrear))
    app.add_handler(CommandHandler("evidencia", cmd_evidencia))
    app.add_handler(CommandHandler("perfil", cmd_perfil))
    app.add_handler(CommandHandler("ficha", cmd_ficha))
    app.add_handler(CommandHandler("preguntar", cmd_preguntar))
    app.add_handler(CommandHandler("ia", cmd_ia))
    app.add_handler(CommandHandler("senales", cmd_senales))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rendimiento", cmd_rendimiento))
    app.add_handler(CommandHandler("estrellasperf", cmd_wallets_perf))
    app.add_handler(CommandHandler("salud", cmd_salud))
    app.add_handler(CommandHandler("datos", cmd_datos))
    app.add_handler(CommandHandler("reevaluar", cmd_reevaluar))
    app.add_handler(CommandHandler("exportar", cmd_exportar))
    app.add_handler(CommandHandler("errores", cmd_errores))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("ialocal", cmd_ialocal))
    app.add_handler(CommandHandler("salidas", cmd_salidas))
    app.add_handler(CommandHandler("postmortem", cmd_postmortem))
    app.add_handler(CommandHandler("hermanas", cmd_hermanas))
    app.add_handler(CommandHandler("adn", cmd_adn))
    app.add_handler(CommandHandler("clusters", cmd_clusters))
    app.add_handler(CommandHandler("prediccion", cmd_prediccion))
    app.add_handler(CommandHandler("lideres", cmd_lideres))
    app.add_handler(CommandHandler("predicciones", cmd_predicciones))
    app.add_handler(CommandHandler("metricas", cmd_metricas))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("elite", cmd_elite))
    app.add_handler(CommandHandler("alpha", cmd_alpha))
    app.add_handler(CommandHandler("atencion", cmd_atencion))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("similar", cmd_similar))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("estrellas", cmd_estrellas))
    app.add_handler(CommandHandler("hipotesis", cmd_hipotesis))
    app.add_handler(CommandHandler("entidad", cmd_entidad))
    app.add_handler(CommandHandler("entidades", cmd_entidad))
    app.add_handler(CommandHandler("saldos", cmd_saldos))
    app.add_handler(CommandHandler("paper", cmd_paper))
    app.add_handler(CommandHandler("topalertas", cmd_top_alertas))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Chat libre: cualquier texto sin comando activa al agente
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_chat))

    # Servidor de webhooks para señales en tiempo real (Fase 2)
    start_webhook_server()
    # LaserStream corre EN PARALELO al webhook: menor latencia y, sobre todo,
    # reanuda desde el último slot tras un reinicio (antes esas señales se
    # perdían). No hay riesgo de alertas dobles: signals.signature es clave
    # primaria, así que la segunda vía que llegue se descarta sola.
    try:
        from laserstream import start as _ls_start
        if _ls_start():
            print("📡 LaserStream activo (webhook sigue como respaldo)")
    except Exception as e:
        print(f"· LaserStream no arrancó: {e}")

    # ── Reloj PERSISTENTE de jobs (Ola 5, auditoria 19/8 - M19) ─────
    # Los timers del job_queue viven en memoria y el supervisor local
    # reinicia el proceso con cada commit: cada deploy disparaba un
    # ciclo COMPLETO 60 s despues de arrancar (7 commits en un dia =
    # ~8 ciclos = purga acelerada de ⭐ + cuota de Helius/Gecko quemada),
    # y weekly_learning (first=3 dias) no corrio NUNCA. Ahora la ultima
    # ejecucion se guarda en settings y el `first` se calcula desde ahi:
    # un reinicio ya no adelanta nada, y un job vencido corre pronto.
    def _reloj_first(nombre: str, intervalo: int, defecto: int) -> int:
        try:
            from db import get_setting
            _c = get_conn()
            try:
                last = float(get_setting(_c, f"job_ts:{nombre}", 0) or 0)
            finally:
                _c.close()
        except Exception:
            return defecto
        if not last:
            # Primera vez: se ancla el reloj YA, para que el tiempo de
            # calendario cuente ENTRE reinicios (sin esto, un job de
            # first largo — weekly_learning, 3 dias — necesitaria 3 dias
            # de proceso ininterrumpido y no corria jamas).
            try:
                from db import set_setting
                _c = get_conn()
                try:
                    set_setting(_c, f"job_ts:{nombre}",
                                _t.time() - intervalo + defecto)
                finally:
                    _c.close()
            except Exception:
                pass
            return defecto
        falta = intervalo - (_t.time() - last)
        if falta <= 0:                      # vencido: corre pronto
            return min(max(defecto, 60), 600)
        return int(min(falta, intervalo))

    def _con_reloj(nombre: str, fn):
        async def _w(ctx):
            try:
                await fn(ctx)
            finally:
                try:
                    from db import set_setting
                    _c = get_conn()
                    try:
                        set_setting(_c, f"job_ts:{nombre}", _t.time())
                    finally:
                        _c.close()
                except Exception:
                    pass
        return _w

    # Ciclo automático: cada N horas DE VERDAD (reloj persistente)
    app.job_queue.run_repeating(
        _con_reloj("auto_cycle", auto_cycle_job),
        interval=AUTO_CYCLE_HOURS * 3600,
        first=_reloj_first("auto_cycle", AUTO_CYCLE_HOURS * 3600, 60),
        name="auto_cycle",
    )
    # Track record: mide el resultado de las señales cada 15 min
    app.job_queue.run_repeating(
        track_outcomes_job,
        interval=900,
        first=120,
        name="track_outcomes",
    )
    # Paper trading: revisa TP/SL/tiempo de las simuladas cada 15 min
    app.job_queue.run_repeating(
        paper_job,
        interval=900,
        first=300,
        name="paper_trading",
    )
    # Motor predictivo: evalúa predicciones vencidas cada 10 min
    app.job_queue.run_repeating(
        predictions_job,
        interval=600,
        first=360,
        name="predictions_eval",
    )
    # Motor de hipótesis: descubrimiento autónomo cada 12 h
    app.job_queue.run_repeating(
        _con_reloj("hypotheses", hypotheses_job),
        interval=12 * 3600,
        first=_reloj_first("hypotheses", 12 * 3600, 1800),
        name="hypotheses",
    )
    # Backup diario de la base + watchdog del webhook + aprendizaje semanal
    app.job_queue.run_repeating(_con_reloj("db_backup", backup_job),
                                interval=86400,
                                first=_reloj_first("db_backup", 86400, 7200),
                                name="db_backup")
    app.job_queue.run_repeating(watchdog_job, interval=3600, first=1800,
                                name="watchdog")
    app.job_queue.run_repeating(
        _con_reloj("weekly_learning", learning_job),
        interval=7 * 86400,
        first=_reloj_first("weekly_learning", 7 * 86400, 3 * 86400),
        name="weekly_learning")
    # Post-mortem (Ola 11): la IA revisa sus decisiones cada 7 días
    app.job_queue.run_repeating(
        _con_reloj("post_mortem", post_mortem_job),
        interval=7 * 86400,
        first=_reloj_first("post_mortem", 7 * 86400, 4 * 86400),
        name="post_mortem")
    # Cierre del ciclo: el rendimiento medido degrada ⭐ cada 24 h
    app.job_queue.run_repeating(
        _con_reloj("performance_review", performance_review_job),
        interval=86400,
        first=_reloj_first("performance_review", 86400, 3600),
        name="performance_review")
    # Autodiagnóstico cada 6 h (solo avisa ante problemas críticos)
    app.job_queue.run_repeating(_con_reloj("salud", salud_job),
                                interval=6 * 3600,
                                first=_reloj_first("salud", 6 * 3600, 900),
                                name="salud")
    # Re-sincroniza el webhook con las ⭐ cada 30 min (nadie sin monitorear)
    app.job_queue.run_repeating(sync_webhook_job, interval=1800, first=300,
                                name="sync_webhook")
    # Resumen diario a las 13:00 UTC (~8am América)
    import datetime as _dt
    _now = _dt.datetime.now(_dt.timezone.utc)
    _target = _now.replace(hour=13, minute=0, second=0, microsecond=0)
    if _target <= _now:
        _target += _dt.timedelta(days=1)
    app.job_queue.run_repeating(daily_summary_job, interval=86400,
                                first=(_target - _now).total_seconds(),
                                name="daily_summary")

    print(f"🤖 Bot corriendo. Ciclo automático cada {AUTO_CYCLE_HOURS:g} h.")
    app.run_polling()


if __name__ == "__main__":
    main()
