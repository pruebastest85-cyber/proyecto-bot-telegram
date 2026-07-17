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
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
AUTO_CYCLE_HOURS = float(os.getenv("AUTO_CYCLE_HOURS", "6"))

# Evita que el ciclo automático y un comando manual corran a la vez
cycle_lock = threading.Lock()

# Acciones del agente pendientes de confirmación (una por usuario)
PENDING_ACTIONS: dict[int, dict] = {}

# Usuarios a los que el hub les pidió un dato (address o pregunta).
# user_id -> nombre del comando ("perfil", "ficha", "preguntar"…)
AWAITING: dict[int, str] = {}


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
                f"Ganadores nuevos/actualizados: {saved}\n"
                f"Billeteras totales: {wallets}\n"
                f"Rastreadas ⭐: {tracked}\n"
                f"📡 {hook_msg}")
    finally:
        cycle_lock.release()


def _status_text() -> str:
    conn = get_conn()
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
    conn.close()
    return (
        f"📊 *Estado del sistema*\n\n"
        f"⚙️ Ciclo automático: cada {AUTO_CYCLE_HOURS:g} h\n"
        f"🎯 Umbral de señal: {float(umbral or 0):.0f}/100\n"
        f"Tokens ganadores: {tokens} ({pend} pendientes)\n"
        f"Billeteras registradas: {wallets}\n"
        f"Billeteras rastreadas ⭐: {tracked}\n"
        f"Descartadas/bots ❌: {descartadas}")


def _senales_text() -> str:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY ts DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        return ("📡 *Últimas señales*\n\n"
                "_Aún no hay señales. Llegarán cuando una billetera ⭐ "
                "compre o venda algo._")
    lines = ["📡 *Últimas señales*", "━━━━━━━━━━━━━━", ""]
    for s in rows:
        hace = (_t.time() - s["ts"]) / 3600
        cuando = f"hace {hace:.1f}h" if hace >= 1 else f"hace {hace*60:.0f} min"
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
        lines.append(
            f"{emoji} *{simbolo}*  ·  {verbo}  ·  {s['sol']:.2f} SOL"
            f"\n    🕒 {cuando}{res}\n")
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
        from maintenance import watchdog_check
        await asyncio.to_thread(watchdog_check)
    except Exception as e:
        print(f"· watchdog_job falló: {e}")


async def learning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import weekly_learning
        await asyncio.to_thread(weekly_learning)
    except Exception as e:
        print(f"· learning_job falló: {e}")


async def track_outcomes_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: mide el resultado (1h/24h) de las señales."""
    try:
        from signal_tracker import track_outcomes
        await asyncio.to_thread(track_outcomes)
    except Exception as e:
        print(f"· track_outcomes falló: {e}")


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
           f"⭐ activas: {stars}"]
    if med["c"]:
        wr = 100.0 * (med["w"] or 0) / med["c"]
        out.append(f"Win rate 7 días: {wr:.0f}% ({med['c']} medidas)")
    out.append("\nDetalle: /rendimiento · Simulación: /backtest")
    return "\n".join(out)


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID:
        return
    try:
        txt = await asyncio.to_thread(_resumen_diario_text)
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt,
                                   parse_mode="Markdown")
    except Exception as e:
        print(f"· resumen diario falló: {e}")


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

    # Botones bajo las alertas de señal
    if data.startswith("ficha:") or data.startswith("saldo1:") \
            or data.startswith("adel:"):
        addr = data.split(":", 1)[1]
        if data.startswith("adel:"):
            ok = await asyncio.to_thread(discard_wallet, addr)
            await q.answer("❌ Descartada" if ok else "No encontrada")
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


@solo_admin
async def cmd_senales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_senales_text(), parse_mode="Markdown")


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
    await update.message.reply_text(_status_text(), parse_mode="Markdown")


@solo_admin
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

async def _post_init(app: Application):
    """Registra el menú de comandos que se ve al pulsar '/' en Telegram."""
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Abrir el menú principal"),
            BotCommand("menu", "Abrir el menú principal"),
            BotCommand("top", "Top de billeteras"),
            BotCommand("senales", "Últimas señales"),
            BotCommand("status", "Estado del sistema"),
            BotCommand("app", "Panel visual (Mini App)"),
            BotCommand("ciclo", "Correr el pipeline ahora"),
            BotCommand("preguntar", "Preguntar a la IA sobre tu base"),
            BotCommand("rendimiento", "Win rate de las señales"),
            BotCommand("backtest", "Simular copiar las señales"),
            BotCommand("saldos", "Saldo SOL de las vigiladas"),
            BotCommand("hermanas", "Billeteras del mismo dueño"),
            BotCommand("ficha", "Ficha completa de una billetera"),
        ])
    except Exception as e:
        print(f"· set_my_commands falló: {e}")


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
        rows = conn.execute(
            """SELECT address, alias, is_tracked FROM wallets
               WHERE is_tracked=1
                  OR (is_bot=0 AND winning_tokens_count >= 2)
               ORDER BY is_tracked DESC, score DESC LIMIT 25""").fetchall()
        conn.close()
        out = ["💰 *Saldos de billeteras vigiladas:*\n"]
        total = 0.0
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
            nombre = (r["alias"] or r["address"][:8]).replace("*", "")
            icono = "⭐" if r["is_tracked"] else "👁"
            if sol is None:
                out.append(f"{icono} {nombre}: _error al consultar_")
            else:
                total += sol
                out.append(f"{icono} {nombre}: *{sol:,.2f} SOL*"
                           f"  `{r['address'][:8]}…`")
        out.append(f"\nTotal combinado: *{total:,.2f} SOL*")

    return "\n".join(out)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Créalo con @BotFather.")
    if not ADMIN_ID:
        print("⚠️  TELEGRAM_ADMIN_ID no configurado: el bot responderá a "
              "CUALQUIERA. Configúralo antes de usarlo en serio.")
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
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
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("hermanas", cmd_hermanas))
    app.add_handler(CommandHandler("saldos", cmd_saldos))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Chat libre: cualquier texto sin comando activa al agente
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_chat))

    # Servidor de webhooks para señales en tiempo real (Fase 2)
    start_webhook_server()

    # Ciclo automático: primero a los 60s de arrancar, luego cada N horas
    app.job_queue.run_repeating(
        auto_cycle_job,
        interval=AUTO_CYCLE_HOURS * 3600,
        first=60,
        name="auto_cycle",
    )
    # Track record: mide el resultado de las señales cada 15 min
    app.job_queue.run_repeating(
        track_outcomes_job,
        interval=900,
        first=120,
        name="track_outcomes",
    )
    # Backup diario de la base + watchdog del webhook + aprendizaje semanal
    app.job_queue.run_repeating(backup_job, interval=86400, first=7200,
                                name="db_backup")
    app.job_queue.run_repeating(watchdog_job, interval=3600, first=1800,
                                name="watchdog")
    app.job_queue.run_repeating(learning_job, interval=7 * 86400,
                                first=3 * 86400, name="weekly_learning")
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
