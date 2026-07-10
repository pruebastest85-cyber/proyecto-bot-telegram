"""
Bot de Telegram — Panel de control del pipeline de billeteras.

Comandos:
  /start       → ayuda
  /ciclo       → corre descubrimiento + análisis completo (tarda minutos)
  /descubrir   → solo busca tokens ganadores nuevos
  /analizar    → solo analiza tokens pendientes
  /top         → mejores 10 billeteras por score
  /evidencia <address> → el "porqué" de una billetera
  /status      → resumen de la base de datos

Configuración:
  1. Habla con @BotFather en Telegram → /newbot → copia el token
  2. export TELEGRAM_BOT_TOKEN="tu_token"
  3. export TELEGRAM_ADMIN_ID="tu_id_numerico"  (obtén el tuyo con @userinfobot)
  4. pip install python-telegram-bot
  5. python telegram_bot.py

El ADMIN_ID es importante: sin él, cualquier persona que encuentre
tu bot podría ejecutar comandos y gastar tus créditos de API.
"""

import asyncio
import os

from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes)

import config
from db import get_conn, top_wallets, wallet_evidence
from discovery import run_discovery
from wallet_analyzer import run_analysis

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))


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
        "/ciclo — descubrimiento + análisis completo\n"
        "/descubrir — buscar tokens ganadores\n"
        "/analizar — analizar compradores tempranos\n"
        "/top — mejores billeteras\n"
        "/evidencia <address> — el porqué de una billetera\n"
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
    await update.message.reply_text(
        "⏳ Analizando compradores tempranos… esto puede tardar varios "
        "minutos según cuántos tokens haya pendientes.")
    await asyncio.to_thread(run_analysis)
    await update.message.reply_text("✅ Análisis terminado. Usa /top para ver resultados.")


@solo_admin
async def cmd_ciclo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Ciclo completo iniciado…")
    saved = await asyncio.to_thread(run_discovery)
    await update.message.reply_text(f"Fase 1 lista: {saved} ganadores. Analizando…")
    await asyncio.to_thread(run_analysis)
    await update.message.reply_text("✅ Ciclo completo terminado. Usa /top.")


@solo_admin
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = top_wallets(conn, 10)
    conn.close()
    if not rows:
        await update.message.reply_text(
            "Aún no hay billeteras. Corre /ciclo primero.")
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
    app.add_handler(CommandHandler("status", cmd_status))
    print("🤖 Bot corriendo. Envíale /start en Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
