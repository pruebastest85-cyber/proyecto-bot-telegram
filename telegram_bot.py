"""
Bot de Telegram — Panel de control del pipeline de billeteras.
v2: ciclo automático cada AUTO_CYCLE_HOURS horas con aviso al admin.

Comandos:
  /start       → ayuda
  /ciclo       → corre descubrimiento + análisis completo ahora
  /descubrir   → solo busca tokens ganadores nuevos
  /analizar    → solo analiza tokens pendientes
  /top         → mejores 10 billeteras por score
  /evidencia <address> → el "porqué" de una billetera
  /status      → resumen de la base de datos

Variables de entorno:
  TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, HELIUS_API_KEY, DB_PATH
  AUTO_CYCLE_HOURS (opcional, default 6)
"""

import asyncio
import os
import threading

from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes)

import config
from db import get_conn, top_wallets, wallet_evidence
from discovery import run_discovery
from wallet_analyzer import run_analysis
from wallet_profiler import profile_wallet, format_profile

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
AUTO_CYCLE_HOURS = float(os.getenv("AUTO_CYCLE_HOURS", "6"))

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
        return (f"✅ Ciclo terminado.\n"
                f"Ganadores nuevos/actualizados: {saved}\n"
                f"Billeteras totales: {wallets}\n"
                f"Rastreadas ⭐: {tracked}")
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


@solo_admin
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 *Wallet Discovery Bot*\n\n"
        f"⚙️ Ciclo automático activo: cada {AUTO_CYCLE_HOURS:g} horas\n\n"
        "/ciclo — descubrimiento + análisis ahora\n"
        "/descubrir — buscar tokens ganadores\n"
        "/analizar — analizar compradores tempranos\n"
        "/top — mejores billeteras\n"
        "/evidencia <address> — el porqué de una billetera\n"
        "/perfil <address> — investigar una billetera a fondo\n"
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
    conn = get_conn()
    rows = top_wallets(conn, 10)
    conn.close()
    if not rows:
        await update.message.reply_text(
            "Aún no hay billeteras. Espera el próximo ciclo o corre /ciclo.")
        return
    lines = ["🏆 *Top billeteras candidatas:*\n"]
    for i, w in enumerate(rows, 1):
        flag = " ⭐" if w["is_tracked"] else ""
        lines.append(
            f"{i}. `{w['address']}`\n"
            f"   ganadores: {w['winning_tokens_count']} · "
            f"score: {w['score']:.1f}{flag}\n")
    lines.append("\nUsa /evidencia <address> para ver el porqué.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    tokens = conn.execute("SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"]
    pend = conn.execute(
        "SELECT COUNT(*) c FROM winning_tokens WHERE analyzed=0").fetchone()["c"]
    wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
    tracked = conn.execute(
        "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
    conn.close()
    await update.message.reply_text(
        f"📊 *Estado del sistema*\n\n"
        f"⚙️ Ciclo automático: cada {AUTO_CYCLE_HOURS:g} h\n"
        f"Tokens ganadores: {tokens} ({pend} pendientes)\n"
        f"Billeteras registradas: {wallets}\n"
        f"Billeteras rastreadas ⭐: {tracked}",
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
    app.add_handler(CommandHandler("evidencia", cmd_evidencia))
    app.add_handler(CommandHandler("perfil", cmd_perfil))
    app.add_handler(CommandHandler("status", cmd_status))

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
