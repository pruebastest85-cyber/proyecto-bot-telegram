"""
Detección de billeteras hermanas: direcciones que probablemente
pertenecen al mismo dueño porque se transfieren SOL directamente.

v1: revisa el historial reciente de cada billetera ⭐ y busca
transferencias nativas de SOL entre billeteras rastreadas.
"""

import time

import requests

import config
from db import get_conn

MIN_SOL_LINK = 0.01   # ignorar polvo
TX_LIMIT = 100        # txs recientes a revisar por billetera


def _apuntar_creditos(n_llamadas: int = 1) -> None:
    """(19-E) Este modulo usa la Enhanced Transactions API, que cuesta
    `HELIUS_CREDITS_PER_CALL` (100) creditos por llamada, y NO importaba
    `api_usage` ni `helius_budget` — verificado con un grep: cero
    coincidencias en todo el archivo.

    `/hermanas` recorre TODAS las ⭐: 1 llamada por billetera en `_txs`
    mas hasta 10 paginas en `_funder`, o sea hasta 11 llamadas = 1.100
    creditos POR ⭐. Con 130 ⭐ eran ~143.000 creditos en una sola
    pulsacion —mas que un dia entero de operacion normal (medido:
    103.000-250.000/dia)— y ninguno aparecia en el contador que alimenta
    el freno del 85% ni en /salud."""
    try:
        from api_usage import record as _api_rec
        _api_rec("helius", n_llamadas)
        _api_rec("helius_credits",
                 n_llamadas * getattr(config, "HELIUS_CREDITS_PER_CALL", 100))
    except Exception as e:
        print(f"· hermanas: no pude apuntar los créditos ({e})")


def _txs(address: str, limit: int = TX_LIMIT):
    url = config.HELIUS_PARSED_TX.format(address=address)
    r = requests.get(url, params={"api-key": config.HELIUS_API_KEY,
                                  "limit": limit}, timeout=30)
    r.raise_for_status()
    _apuntar_creditos()
    return r.json()


def _funder(address: str):
    """Quién le envió el primer SOL conocido (origen de fondeo).

    (19-AB, auditoria BAJO) Sale de `wallet_funding.fondeo`, que pregunta
    a la API funded-by de Helius UNA vez (100 créditos) y lo guarda para
    siempre — en la base del dueño las 178 ⭐ ya lo tenían cacheado.
    Antes esto paginaba hasta 10 páginas del historial (1.000 créditos)
    POR ⭐ en CADA pulsación de /vinculos, para deducir a mano lo mismo.
    """
    try:
        from wallet_funding import fondeo
        d = fondeo(address)
    except Exception as e:
        print(f"· vinculos: no pude leer el fondeo de {address[:8]}: {e}")
        return None
    return (d or {}).get("funder") or None


def find_links() -> str:
    """Busca transferencias directas entre billeteras ⭐ y devuelve
    un informe listo para Telegram."""
    # (19-E) `try/finally`: la conexión se cerraba después del `execute`
    # sin protección, así que una consulta que lanzara la dejaba abierta
    # reteniendo el candado de escritura de SQLite.
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT address, alias FROM wallets WHERE is_tracked=1"
        ).fetchall()
    finally:
        conn.close()
    tracked = {r["address"]: (r["alias"] or r["address"][:8])
               for r in rows}
    if len(tracked) < 2:
        return "🔗 Hacen falta al menos 2 billeteras ⭐ para buscar vínculos."

    # (19-E) FRENO DE PRESUPUESTO antes de empezar. Este comando era el
    # más caro del bot con diferencia: hasta 11 llamadas de la Enhanced
    # API (mil cien créditos) por cada ⭐. (19-AB) Ahora el fondeo sale de la
    # caché de `wallet_funding` y queda 1 llamada (100 créditos) por ⭐
    # para las transferencias directas, más el fondeo de las que aún no
    # lo tengan (100, una vez en la vida).
    try:
        from helius_budget import puede_llamar
        if not puede_llamar():
            return ("🔗 No lo hago ahora: el freno de presupuesto de "
                    "Helius está activo. Este comando puede costar hasta "
                    f"{len(tracked) * 200:,} créditos y el consumo del "
                    "ciclo ya va alto. Mira /salud y vuelve a intentarlo "
                    "cuando baje.")
        print(f"· hermanas: {len(tracked)} ⭐ × 100 créditos (+100 por "
              f"cada fondeo aún no cacheado) ≈ {len(tracked) * 100:,}-"
              f"{len(tracked) * 200:,} créditos")
    except ImportError:
        pass

    links = {}
    revisadas = 0
    # (Ola 8, 21/8) La MISMA transferencia A->B aparece en el historial
    # de A y en el de B: sin dedupe se sumaba DOS veces y el SOL mostrado
    # podia salir duplicado (o x1/x2 segun que historiales bajaran bien).
    vistas = set()
    for addr in tracked:
        try:
            txs = _txs(addr)
            revisadas += 1
        except Exception as e:
            print(f"· hermanas: fallo con {addr[:8]}: {e}")
            continue
        time.sleep(config.HELIUS_DELAY)
        for tx in txs or []:
            firma = tx.get("signature") or ""
            for i, nt in enumerate(tx.get("nativeTransfers") or []):
                a = nt.get("fromUserAccount")
                b = nt.get("toUserAccount")
                if a in tracked and b in tracked and a != b:
                    sol = (nt.get("amount") or 0) / 1e9
                    if sol >= MIN_SOL_LINK:
                        huella = (firma, i, a, b, nt.get("amount"))
                        if huella in vistas:
                            continue
                        vistas.add(huella)
                        k = tuple(sorted((a, b)))
                        links[k] = links.get(k, 0.0) + sol

    # v2: fondeo común — quién les envió su primer SOL conocido
    funders = {}
    for addr in tracked:
        f = _funder(addr)
        if f:
            funders.setdefault(f, []).append(addr)
    grupos = {f: ws for f, ws in funders.items() if len(ws) > 1}

    if not links and not grupos:
        return (f"🔗 Revisé {revisadas} billeteras ⭐ y no encontré "
                "transferencias directas ni fondeo común entre ellas.\n"
                "_Cada una parece ser de un dueño distinto (o usan "
                "intermediarios que aún no detecto)._")

    def _n(a):
        return str(tracked.get(a, a[:8])).replace("*", "").replace("_", " ")

    out = ["🔗 *Posibles billeteras del mismo dueño:*\n"]
    for (a, b), sol in sorted(links.items(), key=lambda x: -x[1]):
        out.append(f"• *{_n(a)}* ↔ *{_n(b)}*: {sol:.2f} SOL entre ellas")
        out.append(f"  `{a[:10]}…` / `{b[:10]}…`")
    for f, ws in grupos.items():
        nombres = " + ".join(f"*{_n(w)}*" for w in ws)
        out.append(f"• Fondeadas por la misma fuente `{str(f)[:10]}…`: "
                   f"{nombres}")
    out.append("\n_Vínculos por transferencias directas y por fondeo común "
               "(quién les envió su primer SOL conocido)._")
    return "\n".join(out)
