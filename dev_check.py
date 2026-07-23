"""
Detector "¿el dev vendió?".

Identifica la billetera creadora del token (el feePayer de su primera
transacción) y revisa si ha vendido/movido su suministro mirando las
transferencias de ese mint en su actividad reciente.

Usa Helius (consume créditos; el usuario lo autorizó). Se ejecuta SOLO
cuando la extracción de compradores realmente corrió, así hereda el
límite por hora y no se dispara el gasto.
"""

from wallet_analyzer import fetch_earliest_txs, fetch_parsed_txs


def find_creator(mint: str, earliest=None) -> str | None:
    """El feePayer de la 1ª transacción del token = creador/deployer."""
    try:
        txs = earliest if earliest else fetch_earliest_txs(mint, max_pages=1)
        if not txs:
            return None
        return txs[0].get("feePayer")
    except Exception as e:
        print(f"· find_creator falló: {e}")
        return None


def dev_status(mint: str, earliest=None) -> dict:
    creator = find_creator(mint, earliest)
    if not creator:
        return {"known": False}
    recibido = 0.0
    enviado = 0.0
    try:
        txs = fetch_parsed_txs(creator, limit=100)
    except Exception as e:
        print(f"· dev_status pull falló: {e}")
        return {"known": True, "creator": creator, "vendio": None, "pct": None}
    for tx in txs or []:
        for tt in (tx.get("tokenTransfers") or []):
            if tt.get("mint") != mint:
                continue
            try:
                amt = float(tt.get("tokenAmount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if tt.get("toUserAccount") == creator:
                recibido += amt
            if tt.get("fromUserAccount") == creator:
                enviado += amt
    if recibido <= 0 and enviado <= 0:
        return {"known": True, "creator": creator, "vendio": None, "pct": None}
    pct = min(100, round(100.0 * enviado / recibido)) if recibido > 0 else None
    return {"known": True, "creator": creator,
            "vendio": enviado > 0, "enviado": enviado,
            "recibido": recibido, "pct": pct}


def dev_line(mint: str, earliest=None) -> str:
    """Línea corta para el mensaje. Cadena vacía si no se pudo determinar."""
    try:
        d = dev_status(mint, earliest)
    except Exception as e:
        print(f"· dev_line falló: {e}")
        return ""
    if not d.get("known"):
        return ""
    if d.get("vendio") is None:
        return "👨‍💻 Dev: no detecté movimientos recientes de su suministro"
    if d.get("vendio"):
        pct = d.get("pct")
        extra = f" (~{pct}% de lo que recibió)" if pct else ""
        return f"⚠️ Dev: vendió/movió parte de su suministro{extra}"
    return "👨‍💻 Dev: conserva su suministro (sin ventas detectadas)"
