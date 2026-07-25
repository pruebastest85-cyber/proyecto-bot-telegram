"""
Exportación del conocimiento acumulado — pensado para analizarlo FUERA
del bot (por ejemplo, con una IA local en tu propio equipo).

La gracia de tener los datos en casa es poder darles vueltas sin límite de
créditos ni de presupuesto de API: un modelo local puede leerse TODO el
historial y buscar patrones que aquí no calculamos.

Genera un único archivo JSON con:
  · billeteras y sus métricas (PnL, win rate, consistencia, grado…)
  · operaciones guardadas (compras/ventas con token, SOL, tokens y fecha)
  · apariciones en tokens ganadores (con el MC de entrada y el crecimiento)
  · señales emitidas y su resultado medido (chg_1h / chg_24h)

Ese último bloque es el más valioso para entrenar o afinar criterios: dice
qué recomendó el sistema y qué pasó después.
"""

import json
import os
import time

from db import get_conn


def _filas(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:
        print(f"· Export: consulta omitida ({e})")
        return []


def exportar(ruta: str | None = None, max_ops: int = 200_000) -> str | None:
    """Vuelca el conocimiento a JSON. Devuelve la ruta del archivo."""
    ruta = ruta or os.path.join(
        os.getenv("EXPORT_DIR", "/tmp"),
        f"wallet_edge_{time.strftime('%Y%m%d_%H%M')}.json")
    conn = get_conn()
    try:
        datos = {
            "generado": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "billeteras": _filas(conn, """
                SELECT address, alias, grade, consistency, wallet_score,
                       pnl_total, pnl_net, pnl_30d, winning_tokens_count,
                       score, ai_class, ai_reason, is_tracked, is_bot,
                       hold_median_min, roi_median
                FROM wallets WHERE COALESCE(is_bot,0)=0"""),
            "operaciones": _filas(conn, """
                SELECT wallet, mint, side, sol, tokens, ts
                FROM trades ORDER BY ts DESC LIMIT ?""", (max_ops,)),
            "apariciones": _filas(conn, """
                SELECT wallet, mint, buy_sol, buy_time, buy_rank, delay_s,
                       price_at_buy, mc_at_buy, entry_multiple
                FROM appearances"""),
            "senales": _filas(conn, """
                SELECT signature, wallet, mint, symbol, side, sol, ts,
                       price_usd, mc, liq, chg_1h, chg_24h, signal_score,
                       verdict FROM signals ORDER BY ts DESC LIMIT 50000"""),
            "tokens_ganadores": _filas(conn, """
                SELECT mint, symbol, price_change_24h, volume_24h_usd,
                       liquidity_usd, detected_at FROM winning_tokens"""),
        }
    finally:
        conn.close()

    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"· No se pudo escribir el export: {e}")
        return None

    print(f"📦 Export: {ruta} · " + " · ".join(
        f"{k}={len(v)}" for k, v in datos.items() if isinstance(v, list)))
    return ruta


def resumen() -> str:
    """Qué contendría el export, sin generarlo."""
    conn = get_conn()
    try:
        def n(tabla, extra=""):
            try:
                return conn.execute(
                    f"SELECT COUNT(*) c FROM {tabla} {extra}").fetchone()["c"]
            except Exception:
                return 0
        w = n("wallets", "WHERE COALESCE(is_bot,0)=0")
        t = n("trades")
        a = n("appearances")
        s = n("signals")
        med = n("signals", "WHERE chg_24h IS NOT NULL OR chg_1h IS NOT NULL")
    finally:
        conn.close()
    return "\n".join([
        "📦 *Conocimiento acumulado*\n",
        f"Billeteras analizadas: *{w:,}*",
        f"Operaciones guardadas: *{t:,}*",
        f"Apariciones en ganadores: *{a:,}*",
        f"Señales emitidas: *{s:,}* · con resultado medido: *{med:,}*",
        "\n_Usa /exportar para bajarlo en JSON y analizarlo por tu cuenta "
        "(por ejemplo con una IA local)._",
    ])
