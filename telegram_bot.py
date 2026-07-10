"""
Bot de Telegram — Panel de control del pipeline de billeteras.
v3: /top con botones inline para descartar billeteras y tamaño 10/20/30.

Comandos:
  /start       → ayuda
  /ciclo       → corre descubrimiento + análisis completo ahora
  /descubrir   → solo busca tokens ganadores nuevos
  /analizar    → solo analiza tokens pendientes
  /top [n]     → mejores billeteras (10 por defecto; botones para 10/20/30)
  /descartar <address> → marcar como bot y dejar de rastrear
  /rastrear <address>  → revertir un descarte
  /evidencia <address> → el "porqué" de una billetera
  /status      → resumen de la base de datos

Variables de entorno:
  TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, HELIUS_API_KEY, DB_PATH
  AUTO_CYCLE_HOURS (opcional, default 6)
"""

import asyncio
import os
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

import config
from db import get_conn, top_wallets, wallet_evidence
from discovery import run_discovery
from wallet_analyzer import run_analysis
from wallet_profiler import profile_wallet, format_profile
from realtime import start_webhook_server, sync_helius_webhook, tracked_addresses

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
AUTO_CYCLE_HOURS = float(os.getenv("AUTO_CYCLE_HOURS", "6"))

TOP_SIZES = (10, 20, 30)

# Evita que el ciclo automático y un comando manual corran a la vez
cycle_lock = threading.Lock()


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


# ══════════════ descartar / restaurar billeteras ══════════════

def discard_wallet(address: str) -> str:
    """Marca la billetera como bot, le quita la ⭐ y resincroniza el webhook."""
    conn = get_conn()
    row = conn.execute("SELECT address FROM wallets WHERE address=?",
                       (address,)).fetchone()
    if not row:
        conn.close()
        return "No existe esa dirección en la base."
    conn.execute(
        """UPDATE wallets SET is_bot=1, is_tracked=0,
           ai_class='descartada', ai_follow=0,
           ai_reason='Descartada manualmente por el admin'
           WHERE address=?""", (address,))
    conn.commit()
    conn.close()
    hook = sync_helius_webhook()
    return f"❌ {address[:8]}… descartada. {hook}"


def restore_wallet(address: str) -> str:
    """Revierte un descarte: vuelve a rastrear y la IA la reevaluará."""
    conn = get_conn()
    row = conn.execute("SELECT address FROM wallets WHERE address=?",
                       (address,)).fetchone()
    if not row:
        conn.close()
        return "No existe esa dirección en la base."
    conn.execute(
        """UPDATE wallets SET is_bot=0, is_tracked=1,
           ai_class=NULL, ai_follow=NULL, ai_reason=NULL
           WHERE address=?""", (address,))
    conn.commit()
    conn.close()
    hook = sync_helius_webhook()
    return f"⭐ {address[:8]}… vuelve a rastrearse. {hook}"


def build_top_message(limit: int = 10):
    """Arma el texto y el teclado inline del /top."""
    conn = get_conn()
    rows = top_wallets(conn, limit)
    conn.close()
    if not rows:
        return ("Aún no hay billeteras. Espera el próximo ciclo o corre /ciclo.",
                None)
    lines = [f"🏆 *Top {len(rows)} billeteras candidatas:*\n"]
    buttons, row_btns = [], []
    for i, w in enumerate(rows, 1):
        flag = " ⭐" if w["is_tracked"] else ""
        try:
            ai = f" · 🧠 {w['ai_class']}" if w["ai_class"] else ""
        except (KeyError, IndexError):
            ai = ""
        lines.append(
            f"{i}. `{w['address']}`\n"
            f"   ganadores: {w['winning_tokens_count']} · "
            f"score: {w['score']:.1f}{flag}{ai}\n")
        row_btns.append(InlineKeyboardButton(
            f"❌ {i}", callback_data=f"d:{limit}:{w['address']}"))
        if len(row_btns) == 5:
            buttons.append(row_btns)
            row_btns = []
    if row_btns:
        buttons.append(row_btns)
    buttons.append([
        InlineKeyboardButton(("· " if n == limit else "") + f"Top {n}",
                             callback_data=f"t:{n}")
        for n in TOP_SIZES
    ])
    lines.append("\n❌ n = descartar la billetera nº n (deja de rastrearse "
                 "y no vuelve al top).\nUsa /evidencia <address> para ver el porqué.")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones inline del /top."""
    q = update.callback_query
    if ADMIN_ID and q.from_user.id != ADMIN_ID:
        await q.answer("⛔ No autorizado", show_alert=True)
        return
    data = q.data or ""
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
        f"⚙️ Ciclo automático activo: cada {AUTO_CYCLE_HOURS:g} horas\n\n"
        "/ciclo — descubrimiento + análisis ahora\n"
        "/descubrir — buscar tokens ganadores\n"
        "/analizar — analizar compradores tempranos\n"
        "/top [n] — mejores billeteras (10/20/30) con botón ❌ para descartar\n"
        "/descartar <address> — dejar de rastrear una billetera\n"
        "/rastrear <address> — revertir un descarte\n"
        "/evidencia <address> — el porqué de una billetera\n"
        "/perfil <address> — investigar una billetera a fondo\n"
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
        from wallet_profiler import profile_wallet
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
    await update.message.reply_text(
        f"🧠 *Veredicto IA para* `{address[:16]}…`\n\n"
        f"Clasificación: *{v['clasificacion'].upper()}*\n"
        f"Recomendación: {icono}\n"
        f"Confianza: {v.get('confianza', '?')}%\n\n"
        f"_{v.get('razon', '')}_",
        parse_mode="Markdown")


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
        lines.append(
            f"• {emoji} {side} `{s['mint'][:12]}…` — {s['sol']:.2f} SOL por "
            f"`{s['wallet'][:8]}…` hace {hace:.1f}h")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    app.add_handler(CommandHandler("ia", cmd_ia))
    app.add_handler(CommandHandler("senales", cmd_senales))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Servidor de webhooks para señales en tiempo real (Fase 2)
    start_webhook_server()

    # Ciclo automático: primero a los 60s de arrancar, luego cada N horas
    app.job_queue.run_repeating(
        auto_cycle_job,
        interval=AUTO_CYCLE_HOURS * 3600,
        first=60,
        name="auto_cycle",
    )

    print(f"🤖 Bot corriendo. Ciclo automático cada {AUTO_CYCLE_HOURS:g} h.")
    app.run_polling()


if __name__ == "__main__":
    main()
