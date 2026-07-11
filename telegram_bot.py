"""
Bot de Telegram — Panel de control del pipeline de billeteras.
v4: alias IA por billetera, PnL 30d/histórico en /top y señales,
    botones inline para descartar y top 10/20/30.

Variables de entorno:
  TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, HELIUS_API_KEY, DB_PATH
  AUTO_CYCLE_HOURS (opcional, default 6)
"""

import asyncio
import os
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


async def track_outcomes_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: mide el resultado (1h/24h) de las señales."""
    try:
        from signal_tracker import track_outcomes
        await asyncio.to_thread(track_outcomes)
    except Exception as e:
        print(f"· track_outcomes falló: {e}")


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


def solo_admin(func):
    """Decorador: ignora mensajes de cualquiera que no sea el dueño."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if ADMIN_ID and update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ No autorizado.")
            return
        return await func(update, ctx)
    return wrapper


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones inline del /top."""
    q = update.callback_query
    if ADMIN_ID and q.from_user.id != ADMIN_ID:
        await q.answer("⛔ No autorizado", show_alert=True)
        return
    data = q.data or ""
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


@solo_admin
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 *Wallet Discovery Bot*\n\n"
        f"⚙️ Ciclo automático activo: cada {AUTO_CYCLE_HOURS:g} horas\n"
        "💬 Escríbeme normal (sin /) y te respondo o ejecuto acciones\n\n"
        "/ciclo — descubrimiento + análisis ahora\n"
        "/descubrir — buscar tokens ganadores\n"
        "/analizar — analizar compradores tempranos\n"
        "/top [n] — mejores billeteras (10/20/30) con botón ❌ para descartar\n"
        "/descartar <address> — dejar de rastrear una billetera\n"
        "/rastrear <address> — revertir un descarte\n"
        "/evidencia <address> — el porqué de una billetera\n"
        "/perfil <address> — investigar una billetera a fondo\n"
        "/ficha <address> — Wallet Score 0-100 con ROI y riesgo\n"
        "/preguntar <texto> — pregúntale a la IA sobre tu base\n"
        "/ia <address> — veredicto de la IA sobre una billetera\n"
        "/senales — últimas señales en tiempo real\n"
        "/status — estado de la base de datos",
        parse_mode="Markdown",
    )


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
    msg = await asyncio.to_thread(discard_wallet, ctx.args[0].strip())
    await update.message.reply_text(msg)


@solo_admin
async def cmd_rastrear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /rastrear <address>")
        return
    msg = await asyncio.to_thread(restore_wallet, ctx.args[0].strip())
    await update.message.reply_text(msg)


@solo_admin
async def cmd_evidencia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /evidencia <address>")
        return
    address = ctx.args[0].strip()
    conn = get_conn()
    rows = wallet_evidence(conn, address)
    conn.close()
    if not rows:
        await update.message.reply_text("Sin registros para esa dirección.")
        return
    lines = [f"📋 *Evidencia de* `{address[:20]}…`:\n"]
    for ev in rows:
        lines.append(f"• {ev['reason']}\n")
    lines.append(f"\n🔗 Verificar: gmgn.ai/sol/address/{address}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@solo_admin
async def cmd_perfil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /perfil <address>")
        return
    address = ctx.args[0].strip()
    await update.message.reply_text(
        "🔬 Investigando billetera… (30-60 segundos)")
    p = await asyncio.to_thread(profile_wallet, address)
    await update.message.reply_text(format_profile(p), parse_mode="Markdown")


@solo_admin
async def cmd_ia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ia <address>")
        return
    address = ctx.args[0].strip()
    await update.message.reply_text("🧠 Perfilando y consultando a la IA… (~1 min)")

    def _run():
        from ai_analyst import ai_verdict
        p = profile_wallet(address)
        if not p["tx_sampled"]:
            return None, None
        conn = get_conn()
        ev = conn.execute(
            "SELECT reason FROM appearances WHERE wallet=? LIMIT 6",
            (address,)).fetchall()
        conn.close()
        return p, ai_verdict(p, [e["reason"] for e in ev])

    p, v = await asyncio.to_thread(_run)
    if not p:
        await update.message.reply_text("Sin transacciones para esa dirección.")
        return
    if not v:
        await update.message.reply_text(
            "La IA no devolvió veredicto (¿ANTHROPIC_API_KEY configurada?).")
        return
    icono = "✅ SEGUIR" if v["seguir"] else "❌ DESCARTAR"
    alias_txt = f"Alias: 👤 *{v['alias']}*\n" if v.get("alias") else ""
    await update.message.reply_text(
        f"🧠 *Veredicto IA para* `{address[:16]}…`\n\n"
        f"{alias_txt}"
        f"Clasificación: *{v['clasificacion'].upper()}*\n"
        f"Recomendación: {icono}\n"
        f"Confianza: {v.get('confianza', '?')}%\n\n"
        f"_{v.get('razon', '')}_",
        parse_mode="Markdown")


@solo_admin
async def cmd_ficha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ficha <address>")
        return
    address = ctx.args[0].strip()
    await update.message.reply_text("🧮 Calculando Wallet Score… (~1 min)")

    def _run():
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
        return format_ficha(address, s, alias, format_track_record(track))

    ficha = await asyncio.to_thread(_run)
    if not ficha:
        await update.message.reply_text("Sin transacciones para esa dirección.")
        return
    await update.message.reply_text(ficha, parse_mode="Markdown")


@solo_admin
async def cmd_preguntar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: /preguntar <pregunta>\nEj: /preguntar ¿qué billetera "
            "tuvo mejor resultado en sus señales?")
        return
    pregunta = " ".join(ctx.args)
    await update.message.reply_text("🤔 Consultando la base…")
    from ai_chat import answer_question
    respuesta = await asyncio.to_thread(answer_question, pregunta)
    await update.message.reply_text(respuesta)


@solo_admin
async def cmd_senales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY ts DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(
            "Sin señales aún. Llegarán cuando una billetera ⭐ compre algo.")
        return
    import time as _t
    lines = ["📡 *Últimas señales:*\n"]
    for s in rows:
        hace = (_t.time() - s["ts"]) / 3600
        try:
            side = s["side"] or "compra"
        except (KeyError, IndexError):
            side = "compra"
        emoji = "🟢" if side == "compra" else "🔴"
        res = ""
        try:
            partes = []
            if s["chg_1h"] is not None:
                partes.append(f"1h: {s['chg_1h']:+.0f}%")
            if s["chg_24h"] is not None:
                partes.append(f"24h: {s['chg_24h']:+.0f}%")
            if partes:
                res = " → " + " · ".join(partes)
        except (KeyError, IndexError):
            pass
        lines.append(
            f"• {emoji} {side} `{s['mint'][:12]}…` — {s['sol']:.2f} SOL por "
            f"`{s['wallet'][:8]}…` hace {hace:.1f}h{res}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@solo_admin
async def on_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chat libre: cualquier mensaje sin /comando activa al agente IA."""
    texto = (update.message.text or "").strip()
    if not texto:
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


@solo_admin
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    tokens = conn.execute("SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"]
    pend = conn.execute(
        "SELECT COUNT(*) c FROM winning_tokens WHERE analyzed=0").fetchone()["c"]
    wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
    tracked = conn.execute(
        "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
    descartadas = conn.execute(
        "SELECT COUNT(*) c FROM wallets WHERE is_bot=1").fetchone()["c"]
    conn.close()
    await update.message.reply_text(
        f"📊 *Estado del sistema*\n\n"
        f"⚙️ Ciclo automático: cada {AUTO_CYCLE_HOURS:g} h\n"
        f"Tokens ganadores: {tokens} ({pend} pendientes)\n"
        f"Billeteras registradas: {wallets}\n"
        f"Billeteras rastreadas ⭐: {tracked}\n"
        f"Descartadas/bots ❌: {descartadas}",
        parse_mode="Markdown",
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Créalo con @BotFather.")
    if not ADMIN_ID:
        print("⚠️  TELEGRAM_ADMIN_ID no configurado: el bot responderá a "
              "CUALQUIERA. Configúralo antes de usarlo en serio.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
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

    print(f"🤖 Bot corriendo. Ciclo automático cada {AUTO_CYCLE_HOURS:g} h.")
    app.run_polling()


if __name__ == "__main__":
    main()
