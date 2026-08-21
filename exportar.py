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

import gzip
import json
import os
import tempfile
import time

from db import get_conn


def _filas(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:
        print(f"· Export: consulta omitida ({e})")
        return []


def _escribir_gz(ruta: str, datos: dict) -> float:
    """Escribe JSON comprimido. Devuelve el tamaño en MB."""
    with gzip.open(ruta, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(datos, fh, ensure_ascii=False, default=str)
    return os.path.getsize(ruta) / 1e6


def exportar(ruta: str | None = None, max_ops: int = 2_000_000,
             limite_mb: float = 45.0) -> list[str]:
    """
    Vuelca el conocimiento a JSON COMPRIMIDO (.json.gz).

    Devuelve una LISTA de rutas. Con 1,5 millones de operaciones el JSON
    plano supera de largo el limite de 50 MB de Telegram, asi que:
      1. se comprime (el JSON baja ~10x)
      2. si aun asi no cabe, se trocea: un archivo base con billeteras,
         apariciones, senales y tokens, y N archivos con las operaciones.

    Cada parte es un JSON valido por si mismo, con 'parte' y 'partes' para
    saber el orden al recomponerlo.
    """
    # tempfile.gettempdir(), no "/tmp" (auditoria 19/8): en el bot local
    # de Windows /tmp no existe y /exportar moria en silencio.
    base_dir = os.getenv("EXPORT_DIR") or tempfile.gettempdir()
    sello = time.strftime("%Y%m%d_%H%M")
    prefijo = ruta.rsplit(".", 1)[0] if ruta else os.path.join(
        base_dir, f"wallet_edge_{sello}")

    conn = get_conn()
    try:
        cabecera = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        billeteras = _filas(conn, """
            SELECT address, alias, grade, consistency, wallet_score,
                   pnl_total, pnl_net, pnl_30d, winning_tokens_count,
                   score, ai_class, ai_reason, is_tracked, is_bot,
                   hold_median_min, roi_median
            FROM wallets WHERE COALESCE(is_bot,0)=0""")
        apariciones = _filas(conn, """
            SELECT wallet, mint, buy_sol, buy_time, buy_rank, delay_s,
                   price_at_buy, mc_at_buy, entry_multiple
            FROM appearances""")
        # (Ola 8, 21/8) Con mas de 100k señales el export recortaba en
        # silencio mientras /datos anunciaba el total completo. El corte
        # se registra y el manifiesto lo dice.
        senales_total = conn.execute(
            "SELECT COUNT(*) c FROM signals").fetchone()["c"]
        senales = _filas(conn, """
            SELECT signature, wallet, mint, symbol, side, sol, ts,
                   price_usd, mc, liq, chg_1h, chg_24h, signal_score,
                   verdict FROM signals ORDER BY ts DESC LIMIT 100000""")
        ganadores = _filas(conn, """
            SELECT mint, symbol, price_change_24h, volume_24h_usd,
                   liquidity_usd, detected_at FROM winning_tokens""")
        operaciones = _filas(conn, """
            SELECT wallet, mint, side, sol, tokens, ts
            FROM trades ORDER BY ts DESC LIMIT ?""", (max_ops,))
    finally:
        conn.close()

    rutas: list[str] = []
    try:
        # Intento 1: todo junto comprimido. Si cabe, un solo archivo.
        unico = f"{prefijo}.json.gz"
        nota_senales = (f"ultimas {len(senales)} de {senales_total}"
                        if senales_total > len(senales) else "completas")
        mb = _escribir_gz(unico, {
            "generado": cabecera, "parte": 1, "partes": 1,
            "senales_alcance": nota_senales,
            "billeteras": billeteras, "apariciones": apariciones,
            "senales": senales, "tokens_ganadores": ganadores,
            "operaciones": operaciones})
        if mb <= limite_mb:
            print(f"📦 Export: {unico} · {mb:.1f} MB · "
                  f"billeteras={len(billeteras)} operaciones={len(operaciones)} "
                  f"senales={len(senales)}")
            return [unico]

        # Intento 2: trocear. Las operaciones son el grueso.
        os.remove(unico)
        print(f"· Export de {mb:.1f} MB supera {limite_mb} MB → troceando")

        base_ruta = f"{prefijo}_1_base.json.gz"
        mb_base = _escribir_gz(base_ruta, {
            "generado": cabecera, "parte": 1,
            "senales_alcance": nota_senales,
            "billeteras": billeteras, "apariciones": apariciones,
            "senales": senales, "tokens_ganadores": ganadores})
        rutas.append(base_ruta)

        # Cuantas operaciones caben por trozo, estimado con lo ya medido.
        por_trozo = max(50_000, int(len(operaciones) * (limite_mb / mb) * 0.8))
        trozos = [operaciones[i:i + por_trozo]
                  for i in range(0, len(operaciones), por_trozo)] or [[]]
        total = len(trozos) + 1
        for i, trozo in enumerate(trozos, start=2):
            r = f"{prefijo}_{i}_operaciones.json.gz"
            _escribir_gz(r, {"generado": cabecera, "parte": i,
                             "partes": total, "operaciones": trozo})
            rutas.append(r)

        print(f"📦 Export en {len(rutas)} partes · base {mb_base:.1f} MB · "
              f"{len(operaciones)} operaciones en {len(trozos)} trozos")
        return rutas
    except Exception as e:
        print(f"· No se pudo escribir el export: {e}")
        return rutas


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
        f"Billeteras registradas: *{w:,}*",
        f"Operaciones guardadas: *{t:,}*",
        f"Apariciones en ganadores: *{a:,}*",
        f"Señales emitidas: *{s:,}* · con resultado medido: *{med:,}*",
        ("\n_Usa /exportar para bajarlo en JSON y analizarlo por tu cuenta "
         "(por ejemplo con una IA local)._"
         + (f"\n_El export incluye las últimas 100.000 señales "
            f"de las {s:,}._" if s > 100000 else "")),
    ])
