"""
Extracción de compradores de un token ENVIADO por el usuario.

Le aplica el MISMO análisis que a un token ganador (wallet_analyzer.analyze_token):
descarga el historial con Helius, extrae los compradores tempranos y los guarda
en `appearances`, alimentando la red de billeteras. Los nuevos entran solos al
grading/IA en el siguiente ciclo automático.

Protecciones para no quemar la cuota de Helius:
  - caché por mint: no re-analizar el mismo token dentro de TOKEN_EXTRACT_CACHE_H horas,
  - límite por hora: máximo TOKEN_EXTRACT_MAX_HORA análisis de tokens enviados por hora.

No modifica el embudo existente: solo reutiliza analyze_token y guarda apariciones.
"""

import os
import time

from db import get_conn, get_setting, set_setting


def _cache_h() -> int:
    try:
        return int(os.getenv("TOKEN_EXTRACT_CACHE_H", "12"))
    except ValueError:
        return 12


def _max_hora() -> int:
    try:
        return int(os.getenv("TOKEN_EXTRACT_MAX_HORA", "5"))
    except ValueError:
        return 5


def _recently_analyzed(mint: str) -> bool:
    conn = get_conn()
    try:
        raw = get_setting(conn, f"tokx:{mint}", "")
    finally:
        conn.close()
    if not raw:
        return False
    try:
        return (time.time() - float(raw)) < _cache_h() * 3600
    except (TypeError, ValueError):
        return False


def _mark(mint: str):
    conn = get_conn()
    try:
        set_setting(conn, f"tokx:{mint}", time.time())
    finally:
        conn.close()


def _rate_ok() -> bool:
    """Cupo por hora, compartido entre todos los tokens enviados."""
    hour = time.strftime("%Y%m%d%H")
    key = f"tokxrate:{hour}"
    conn = get_conn()
    try:
        n = int(float(get_setting(conn, key, "0") or 0))
        if n >= _max_hora():
            return False
        set_setting(conn, key, n + 1)
    finally:
        conn.close()
    return True


def _devolver_cupo() -> None:
    """(19-AP) Un intento que NO analizo nada (Helius caido, sin precio)
    no gasta cupo: antes, con Helius caido, al 6º intento el dueño leia
    "Alcancé el límite" sin que se hubiera registrado nada."""
    hour = time.strftime("%Y%m%d%H")
    key = f"tokxrate:{hour}"
    conn = get_conn()
    try:
        n = int(float(get_setting(conn, key, "0") or 0))
        set_setting(conn, key, max(0, n - 1))
    except (TypeError, ValueError) as e:
        print(f"· token_extract: no pude devolver el cupo ({e})")
    finally:
        conn.close()


def extract_buyers(mint: str, symbol: str | None = None,
                   price_change_24h=None) -> tuple[str, int]:
    """
    Corre analyze_token sobre un mint enviado.
    Devuelve (status, registradas):
      - ("cache", 0): ya se analizó hace poco → no se repite.
      - ("rate", 0):  se alcanzó el límite por hora.
      - ("sin_precio", 0): (19-AJ) DexScreener no da precio actual; no se
        analiza ni se cachea.
      - ("descarga", 0): la descarga de Helius fallo; no se cachea.
      - ("ok", n):    n billeteras registradas en la red.
      - ("error", 0): algo falló (ya logueado).
    """
    if _recently_analyzed(mint):
        return ("cache", 0)
    if not _rate_ok():
        return ("rate", 0)
    try:
        from wallet_analyzer import analyze_token
        conn = get_conn()
        try:
            token = {
                "mint": mint,
                "symbol": symbol or mint[:8],
                "price_change_24h": float(price_change_24h or 0.0),
            }
            registradas = analyze_token(conn, token)
            # (Ola 18-D) Si la descarga fallo, `analyze_token` no registra
            # nada y deja el token pendiente a proposito. Marcarlo aqui lo
            # bloqueaba 12 h (`TOKEN_EXTRACT_CACHE_H`) y ademas se le decia
            # al dueño "0 billeteras registradas", como si el token no
            # tuviera compradores. Un mint pegado a mano no esta en
            # `winning_tokens`, asi que el ciclo tampoco lo reintenta: se
            # perdia del todo.
            from wallet_analyzer import motivo_fallo_descarga, sin_precio_actual
            _motivo = motivo_fallo_descarga()
            _sin_precio = sin_precio_actual()
        finally:
            conn.close()
        if _sin_precio:
            # (19-AJ) Sin precio actual no se analiza (no se puede saber
            # quien llego tarde) y no se gasta nada: que el dueño lo sepa
            # y pueda volver a pegar el mint mas tarde.
            print("· extract_buyers: sin precio actual en DexScreener; "
                  "no se analiza ni se cachea")
            _devolver_cupo()
            return ("sin_precio", 0)
        if _motivo:
            print(f"· extract_buyers: descarga incompleta ({_motivo}); "
                  f"no se cachea, se puede reintentar")
            _devolver_cupo()
            return ("descarga", 0)
        _mark(mint)
        return ("ok", registradas)
    except Exception as e:
        print(f"· extract_buyers falló: {e}")
        return ("error", 0)
