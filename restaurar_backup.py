"""
Restaura un backup del bot (el JSON que manda /backup por Telegram).

Existe porque el 9 de agosto de 2026 se llenó el volumen de Postgres, hubo
que recrear la base, y descubrimos que NO había forma de leer los backups:
backup.py decía "restaurable con un script de import" y ese script nunca se
escribió. Se recuperó por otra vía, pero pudo costar todo el histórico.

Uso:
    python restaurar_backup.py wallets_backup_20260809_1830.json.gz
    python restaurar_backup.py backup.json --solo wallets,signals
    python restaurar_backup.py backup.json --simular

Por defecto NO borra nada: inserta con ON CONFLICT DO NOTHING, así que se
puede correr sobre una base con datos sin machacar lo que ya está.
"""

import argparse
import gzip
import io
import json
import os
import sys

from db import get_conn, USE_PG

# Tablas que el backup puede traer. Se restauran en este orden para que las
# que otras referencian entren primero.
ORDEN = ["wallets", "winning_tokens", "appearances", "signals", "positions",
         "trades", "predictions", "paper_trades", "settings", "chat_history",
         "wallet_identity", "wallet_funding", "submitted_tokens", "errors"]

LOTE = 2000


def _es_gzip(ruta: str) -> bool:
    try:
        with open(ruta, "rb") as fh:
            return fh.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _cabecera(ruta: str, n: int = 16) -> bytes:
    """Primeros bytes del archivo, descomprimiendo si hace falta."""
    try:
        if _es_gzip(ruta):
            with gzip.open(ruta, "rb") as fh:
                return fh.read(n)
        with open(ruta, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _es_sqlite(ruta: str) -> bool:
    """El backup de SQLite es un .db binario, no JSON.

    (Ola 17-C, auditoria 4) El job automatico COMPRIME la copia y deja un
    `wallets_backup_*.db.gz`. Mirar los 16 primeros bytes del archivo tal
    cual daba `\x1f\x8b...` (firma gzip), asi que esto respondia False,
    `_abrir` lo trataba como JSON de texto y `json.load` reventaba con
    UnicodeDecodeError. Resultado: el unico respaldo que existia de
    verdad NO se podia restaurar con la herramienta de restaurar.
    """
    return _cabecera(ruta).startswith(b"SQLite format 3")


def _leer_sqlite(ruta: str) -> dict:
    """Convierte un backup .db de SQLite al mismo diccionario que el JSON,
    para que el resto del script no tenga que distinguir formatos."""
    import sqlite3
    import shutil
    import tempfile
    tmp_descomprimido = None
    if _es_gzip(ruta):
        # sqlite3 no abre un .gz: se descomprime a un temporal y se borra.
        fd, tmp_descomprimido = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        print("   (descomprimiendo el .gz antes de leerlo)")
        with gzip.open(ruta, "rb") as origen, \
                open(tmp_descomprimido, "wb") as destino:
            shutil.copyfileobj(origen, destino, 1024 * 1024)
        ruta = tmp_descomprimido
    src = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    datos = {}
    try:
        tablas = [r["name"] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()]
        for t in tablas:
            try:
                datos[t] = [dict(r) for r in src.execute(
                    f"SELECT * FROM {t}").fetchall()]
            except Exception as e:
                print(f"  · {t}: no se pudo leer del backup ({e})")
    finally:
        src.close()
        if tmp_descomprimido:
            try:
                os.remove(tmp_descomprimido)
            except OSError:
                pass
    return datos


def _abrir(ruta: str):
    """Abre el backup, comprimido o no."""
    if ruta.endswith(".gz"):
        return gzip.open(ruta, "rt", encoding="utf-8")
    return io.open(ruta, "r", encoding="utf-8")


def _columnas_reales(conn, tabla: str) -> set:
    """Columnas que EXISTEN de verdad en la tabla, para no fallar si el
    backup viene de una versión con más o menos campos."""
    try:
        if USE_PG:
            filas = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name=%s", (tabla,)).fetchall()
            return {r["column_name"] for r in filas}
        filas = conn.execute(f"PRAGMA table_info({tabla})").fetchall()
        return {r["name"] for r in filas}
    except Exception:
        return set()


def _insertar(conn, tabla: str, filas: list, simular: bool) -> tuple:
    """Inserta las filas. Devuelve (insertadas, omitidas)."""
    if not filas:
        return (0, 0)
    reales = _columnas_reales(conn, tabla)
    if not reales:
        print(f"  · {tabla}: no existe en esta base, omitida")
        return (0, len(filas))

    cols = [c for c in filas[0].keys() if c in reales]
    if not cols:
        print(f"  · {tabla}: ninguna columna coincide, omitida")
        return (0, len(filas))
    perdidas = set(filas[0].keys()) - set(cols)
    if perdidas:
        print(f"  · {tabla}: se ignoran columnas ausentes {sorted(perdidas)}")

    if simular:
        return (len(filas), 0)

    marca = "%s" if USE_PG else "?"
    sql = (f"INSERT INTO {tabla} ({','.join(cols)}) "
           f"VALUES ({','.join([marca] * len(cols))}) "
           f"ON CONFLICT DO NOTHING")
    puestas = 0
    for i in range(0, len(filas), LOTE):
        lote = [tuple(f.get(c) for c in cols) for f in filas[i:i + LOTE]]
        try:
            cur = conn.executemany(sql, lote)
            puestas += (cur.rowcount if cur.rowcount and cur.rowcount > 0
                        else len(lote))
            conn.commit()
        except Exception as e:
            # Un lote malo no debe tumbar la restauración entera: se
            # reintenta fila a fila para salvar todo lo salvable.
            conn.rollback() if hasattr(conn, "rollback") else None
            print(f"  · {tabla}: lote con error ({e}); fila a fila")
            for fila in lote:
                try:
                    conn.execute(sql, fila)
                    puestas += 1
                except Exception:
                    pass
            conn.commit()
    return (puestas, len(filas) - puestas)


# Tablas con `id` autonumerico en Postgres. Ver _resetear_secuencias.
SERIAL_TABLES = [("appearances", "id"), ("signals", "id"),
                 ("predictions", "id"), ("paper_trades", "id"),
                 ("paper_fills", "id"), ("chat_history", "id"),
                 ("errors", "id"), ("radar_tokens", "id")]


def _resetear_secuencias(conn):
    """(Ola 17-C, auditoria 4) Sin esto la restauracion dejaba una bomba.

    El backup trae la columna `id` explicita, asi que las filas entran con
    sus ids originales — pero la SECUENCIA de Postgres se queda en 1.
    A partir de ahi cada INSERT nuevo pide id=1, 2, 3... que ya existen, y
    como practicamente todas las escrituras son `INSERT OR IGNORE` (que
    db._translate convierte en `ON CONFLICT DO NOTHING`), **la fila nueva
    se descarta EN SILENCIO**. El bot pareceria funcionar mientras deja de
    guardar apariciones, señales y predicciones nuevas.
    `migrate_to_pg.py` ya hacia esto; el restaurador se quedo sin ello.
    """
    if not USE_PG:
        return                      # SQLite usa rowid, no hay secuencias
    arregladas = 0
    for t, idcol in SERIAL_TABLES:
        try:
            conn.execute(
                "SELECT setval(pg_get_serial_sequence(%s, %s), "
                "COALESCE((SELECT MAX(" + idcol + ") FROM " + t + "), 1))",
                (t, idcol))
            arregladas += 1
        except Exception as e:
            print(f"  · secuencia de {t}: aviso ({e})")
    conn.commit()
    print(f"  🔢 Secuencias reajustadas: {arregladas}/{len(SERIAL_TABLES)} "
          f"(sin esto, las filas NUEVAS se descartarian en silencio)")


def restaurar(ruta: str, solo=None, simular=False) -> dict:
    if not os.path.exists(ruta):
        print(f"No existe el archivo: {ruta}")
        return {}
    print(f"📥 Leyendo {ruta}"
          + ("  (SIMULACRO, no se escribe nada)" if simular else ""))
    if _es_sqlite(ruta):
        print("   formato: SQLite (.db)")
        datos = _leer_sqlite(ruta)
    else:
        print("   formato: JSON" + (" comprimido" if ruta.endswith(".gz")
                                    else ""))
        with _abrir(ruta) as fh:
            datos = json.load(fh)

    if not isinstance(datos, dict):
        print("El backup no tiene el formato esperado.")
        return {}

    tablas = [t for t in ORDEN if t in datos]
    tablas += [t for t in datos if t not in tablas
               and isinstance(datos[t], list)]
    if solo:
        pedidas = {x.strip() for x in solo.split(",") if x.strip()}
        tablas = [t for t in tablas if t in pedidas]

    conn = get_conn()
    resumen = {}
    try:
        for t in tablas:
            filas = datos.get(t)
            if isinstance(filas, dict) and "_error" in filas:
                print(f"  · {t}: el backup la guardó con error, omitida")
                continue
            if not isinstance(filas, list):
                continue
            puestas, fuera = _insertar(conn, t, filas, simular)
            resumen[t] = puestas
            print(f"  {t:<18} {puestas:>8} filas"
                  + (f"  ({fuera} omitidas)" if fuera else ""))
        if not simular:
            _resetear_secuencias(conn)
    finally:
        conn.close()

    total = sum(resumen.values())
    print(f"\n{'Simulacro:' if simular else '✅ Restauradas'} "
          f"{total} filas en {len(resumen)} tablas")
    return resumen


def main():
    p = argparse.ArgumentParser(description="Restaura un backup del bot")
    p.add_argument("archivo", help="ruta del .json o .json.gz")
    p.add_argument("--solo", help="solo estas tablas, separadas por comas")
    p.add_argument("--simular", action="store_true",
                   help="no escribe nada, solo dice qué haría")
    a = p.parse_args()
    resumen = restaurar(a.archivo, a.solo, a.simular)
    sys.exit(0 if resumen else 1)


if __name__ == "__main__":
    main()
