"""
Gestión manual de billeteras y armado del mensaje /top.

  - discard_wallet / restore_wallet: descartar una billetera (deja de
    rastrearse y sale del top) o revertir el descarte. Ambas
    resincronizan el webhook de Helius al instante.
  - build_top_message: arma el texto del /top con alias IA, PnL y
    teclado inline (❌ para descartar, tamaños 10/20/30).
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import get_conn, top_wallets
from realtime import sync_helius_webhook

TOP_SIZES = (10, 20, 30)


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
    # ai_follow=1: sin esto, recompute_scores retiraba la ⭐ restaurada en
    # el siguiente ciclo antes de que la IA la reevaluara. ai_class=NULL
    # fuerza la reevaluacion igualmente.
    conn.execute(
        """UPDATE wallets SET is_bot=0, is_tracked=1,
           ai_class=NULL, ai_follow=1,
           ai_reason='Restaurada manualmente por el admin'
           WHERE address=?""", (address,))
    conn.commit()
    conn.close()
    hook = sync_helius_webhook()
    return f"⭐ {address[:8]}… vuelve a rastrearse. {hook}"


def _campo(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


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
        ai_class = _campo(w, "ai_class")
        ai = f" · 🧠 {ai_class}" if ai_class else ""
        alias = _campo(w, "alias")
        ws = _campo(w, "wallet_score")
        etiqueta_ws = f" · 🧮 {ws:.0f}/100" if ws is not None else ""
        nombre = f"👤 *{alias}*{etiqueta_ws}\n   " if alias else ""
        partes = []
        p30, ptot = _campo(w, "pnl_30d"), _campo(w, "pnl_total")
        if p30 is not None:
            partes.append(f"30d: {p30:+.1f}")
        if ptot is not None:
            partes.append(f"hist: {ptot:+.1f}")
        pnl = ("\n   💰 PnL (SOL) " + " · ".join(partes)) if partes else ""
        lines.append(
            f"{i}. {nombre}`{w['address']}`\n"
            f"   ganadores: {w['winning_tokens_count']} · "
            f"score: {w['score']:.1f}{flag}{ai}{pnl}\n")
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
