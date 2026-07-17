"""
Rendimiento del sistema de señales: reporte numérico y backtest.

Usa las señales de compra ya medidas (chg_1h / chg_24h) para calcular
win rates por billetera, clase, hora del día y tamaño de compra, y para
simular cuánto habrías ganado copiando cada señal con un monto fijo.
"""

from datetime import datetime, timezone

from db import get_conn


def _pct(n, d):
    return (100.0 * n / d) if d else 0.0


def _medidas(conn):
    """Señales de compra con resultado medido."""
    return [dict(r) for r in conn.execute(
        """SELECT s.wallet, s.mint, s.symbol, s.sol, s.ts,
                  s.chg_1h, s.chg_24h, s.signal_score,
                  w.alias, w.ai_class, w.is_tracked
           FROM signals s LEFT JOIN wallets w ON w.address = s.wallet
           WHERE s.side = 'compra'
             AND (s.chg_24h IS NOT NULL OR s.chg_1h IS NOT NULL)
           ORDER BY s.ts DESC""").fetchall()]


def _res(r):
    """% resultado de la señal: prioriza 24h, cae a 1h."""
    return r["chg_24h"] if r["chg_24h"] is not None else r["chg_1h"]


def _grupo(filas):
    """(n, win rate %, media %) de una lista de señales."""
    if not filas:
        return 0, 0.0, 0.0
    res = [_res(r) for r in filas]
    wins = sum(1 for x in res if x > 0)
    return len(filas), _pct(wins, len(filas)), sum(res) / len(res)


def rendimiento_text() -> str:
    conn = get_conn()
    filas = _medidas(conn)
    conn.close()
    if len(filas) < 3:
        return ("📊 Aún hay pocas señales medidas "
                f"({len(filas)}). Vuelve a intentar cuando el bot lleve "
                "más tiempo rastreando.")

    n, wr, media = _grupo(filas)
    out = ["📊 *Rendimiento de señales* "
           f"({n} medidas)\n",
           f"Win rate: *{wr:.0f}%* · resultado medio: {media:+.1f}%\n"]

    # ── Por clase de billetera ──
    out.append("*Por clase:*")
    clases = {}
    for r in filas:
        clases.setdefault(r["ai_class"] or "sin clase", []).append(r)
    for c, fs in sorted(clases.items(), key=lambda x: -len(x[1])):
        cn, cwr, cm = _grupo(fs)
        out.append(f"  {c}: {cwr:.0f}% de {cn} ({cm:+.1f}%)")

    # ── Por hora del día (UTC, bloques de 4h) ──
    out.append("\n*Por franja horaria (UTC):*")
    franjas = {}
    for r in filas:
        h = datetime.fromtimestamp(r["ts"], tz=timezone.utc).hour // 4 * 4
        franjas.setdefault(h, []).append(r)
    for h in sorted(franjas):
        fn, fwr, fm = _grupo(franjas[h])
        out.append(f"  {h:02d}-{h+4:02d}h: {fwr:.0f}% de {fn}")

    # ── Por tamaño de compra ──
    out.append("\n*Por tamaño de la compra:*")
    tams = {"<1 SOL": [], "1-5 SOL": [], ">5 SOL": []}
    for r in filas:
        s = r["sol"] or 0
        k = "<1 SOL" if s < 1 else ("1-5 SOL" if s <= 5 else ">5 SOL")
        tams[k].append(r)
    for k, fs in tams.items():
        if fs:
            tn, twr, tm = _grupo(fs)
            out.append(f"  {k}: {twr:.0f}% de {tn} ({tm:+.1f}%)")

    # ── Mejores y peores billeteras (mínimo 3 señales) ──
    porw = {}
    for r in filas:
        porw.setdefault(r["wallet"], []).append(r)
    stats = []
    for w, fs in porw.items():
        if len(fs) >= 3:
            wn, wwr, wm = _grupo(fs)
            alias = (fs[0]["alias"] or w[:8]).replace("*", "").replace("_", " ")
            estrella = "⭐" if fs[0]["is_tracked"] else ""
            stats.append((wwr, wn, wm, alias, estrella))
    if stats:
        stats.sort(reverse=True)
        out.append("\n*Mejores billeteras (≥3 señales):*")
        for wwr, wn, wm, alias, e in stats[:5]:
            out.append(f"  {e}{alias}: {wwr:.0f}% de {wn} ({wm:+.1f}%)")
        if len(stats) > 5:
            out.append("\n*Peores:*")
            for wwr, wn, wm, alias, e in stats[-3:]:
                out.append(f"  {e}{alias}: {wwr:.0f}% de {wn} ({wm:+.1f}%)")

    out.append("\n_Usa /backtest [monto] para simular copiar las señales._")
    return "\n".join(out)


def backtest_text(monto: float = 0.5, tp: float = 50.0) -> str:
    """Simula copiar cada señal medida con `monto` SOL.
    Regla de salida: si el token llegó a +tp% se vende ahí;
    si no, se vende al cierre de 24h (o 1h si es lo único medido)."""
    conn = get_conn()
    filas = _medidas(conn)
    conn.close()
    if len(filas) < 3:
        return f"📉 Solo {len(filas)} señales medidas; aún no da para backtest."

    invertido = gan = 0.0
    wins = 0
    for r in filas:
        res = _res(r)
        salida = tp if (r["chg_24h"] is not None and r["chg_24h"] >= tp) else res
        pnl = monto * salida / 100.0
        invertido += monto
        gan += pnl
        if pnl > 0:
            wins += 1

    final = invertido + gan
    wr = _pct(wins, len(filas))
    icono = "🟢" if gan >= 0 else "🔴"
    return ("🧪 *Backtest de señales*\n\n"
            f"Señales copiadas: {len(filas)} × {monto} SOL\n"
            f"Regla de salida: vender a +{tp:.0f}% o al cierre de 24h\n\n"
            f"Invertido: {invertido:.2f} SOL\n"
            f"Resultado: {icono} {gan:+.2f} SOL ({_pct(gan, invertido):+.1f}%)\n"
            f"Capital final: {final:.2f} SOL · win rate {wr:.0f}%\n\n"
            "_Aproximación con datos de 1h/24h; no incluye fees ni slippage._")
