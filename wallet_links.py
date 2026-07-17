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


def _txs(address: str, limit: int = TX_LIMIT):
    url = config.HELIUS_PARSED_TX.format(address=address)
    r = requests.get(url, params={"api-key": config.HELIUS_API_KEY,
                                  "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()


def _funder(address: str):
    """Quién le envió el primer SOL conocido (mira hasta ~1000 txs
    hacia atrás). Aproximación del origen de fondeo."""
    txs, before = [], None
    try:
        for _ in range(10):
            params = {"api-key": config.HELIUS_API_KEY, "limit": 100}
            if before:
                params["before"] = before
            r = requests.get(config.HELIUS_PARSED_TX.format(address=address),
                             params=params, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            txs = batch  # nos quedamos con el lote más antiguo
            before = batch[-1].get("signature")
            time.sleep(config.HELIUS_DELAY)
            if len(batch) < 100:
                break
    except Exception:
        return None
    for tx in reversed(txs):  # de la más vieja hacia adelante
        for nt in tx.get("nativeTransfers") or []:
            if nt.get("toUserAccount") == address and \
               nt.get("fromUserAccount") not in (None, address) and \
               (nt.get("amount") or 0) / 1e9 >= 0.05:
                return nt.get("fromUserAccount")
    return None


def find_links() -> str:
    """Busca transferencias directas entre billeteras ⭐ y devuelve
    un informe listo para Telegram."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT address, alias FROM wallets WHERE is_tracked=1").fetchall()
    conn.close()
    tracked = {r["address"]: (r["alias"] or r["address"][:8])
               for r in rows}
    if len(tracked) < 2:
        return "🔗 Hacen falta al menos 2 billeteras ⭐ para buscar vínculos."

    links = {}
    revisadas = 0
    for addr in tracked:
        try:
            txs = _txs(addr)
            revisadas += 1
        except Exception as e:
            print(f"· hermanas: fallo con {addr[:8]}: {e}")
            continue
        time.sleep(config.HELIUS_DELAY)
        for tx in txs or []:
            for nt in tx.get("nativeTransfers") or []:
                a = nt.get("fromUserAccount")
                b = nt.get("toUserAccount")
                if a in tracked and b in tracked and a != b:
                    sol = (nt.get("amount") or 0) / 1e9
                    if sol >= MIN_SOL_LINK:
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
