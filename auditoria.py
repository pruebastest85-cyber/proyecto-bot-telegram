"""
Auditoría automática del código. Ejecutar:  python3 auditoria.py

Valida CLASES ENTERAS de error de una pasada, en vez de revisar a ojo.
Cada clase nació de un bug real que se coló en producción:

  1. SQL contra el esquema real  → tabla/columna inexistente.
  2. Placeholders vs parámetros  → UPDATE desalineado (datos en la columna equivocada).
  3. Campos leídos fuera de su SELECT → el bug del "plan de salida": la columna
     existe en la base, pero la consulta no la pedía y la función fallaba en
     silencio (la línea nunca aparecía y no saltaba ningún error).
  4. Literales de estado escritos vs filtrados → el bug de 'Observacion' sin
     tilde: la wallet degradada desaparecía del listado /elite.

Salida: 0 si todo está bien, 1 si hay hallazgos.
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata

RAIZ = os.path.dirname(os.path.abspath(__file__))
FILES = [f for f in sorted(os.listdir(RAIZ)) if f.endswith(".py")
         and f != "auditoria.py"]
SQLRE = re.compile(r"(SELECT|INSERT|UPDATE|DELETE)\s", re.I)
fallos = []


def _base():
    """Base temporal con el esquema real para validar las consultas."""
    os.environ.setdefault("DB_PATH", tempfile.mktemp(suffix=".db"))
    sys.path.insert(0, RAIZ)
    import db as dbmod
    c = dbmod.get_conn()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, "
              "value TEXT)")
    # Tablas que los módulos crean de forma PEREZOSA (no están en el
    # esquema central): se descubren leyendo el propio código, así el
    # auditor no hay que tocarlo cada vez que se añade una tabla nueva.
    for fn in FILES:
        try:
            txt = open(os.path.join(RAIZ, fn)).read()
        except OSError:
            continue
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+\w+\s*\(",
                             txt):
            # Buscar el paréntesis de cierre REAL contando anidamiento:
            # definiciones como "PRIMARY KEY (a, b))" tienen paréntesis
            # dentro y un corte ingenuo generaba SQL inválido (y con ello
            # falsos positivos por "tabla inexistente").
            i = m.end() - 1
            nivel, fin = 0, None
            for j in range(i, min(len(txt), i + 4000)):
                if txt[j] == "(":
                    nivel += 1
                elif txt[j] == ")":
                    nivel -= 1
                    if nivel == 0:
                        fin = j + 1
                        break
            if not fin:
                continue
            try:
                c.execute(txt[m.start():fin])
            except Exception:
                pass
    c.commit()
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.row_factory = sqlite3.Row
    return raw


def _arboles():
    for fn in FILES:
        try:
            yield fn, ast.parse(open(os.path.join(RAIZ, fn)).read(), fn)
        except SyntaxError as e:
            fallos.append(f"{fn}: no compila — {e}")


def clase1_sql(raw):
    """SQL contra el esquema real."""
    n = 0
    for fn, tree in _arboles():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            s = node.value.strip()
            if not SQLRE.match(s) or "{" in s or "%s" in s:
                continue
            try:
                raw.execute("EXPLAIN " + re.sub(r"\?", "NULL", s))
                n += 1
            except sqlite3.Error as e:
                if "no such table" in str(e) or "no such column" in str(e):
                    fallos.append(f"{fn}:{node.lineno} SQL inválido — {e}")
    return n


def clase2_params():
    """Placeholders ? vs parámetros pasados."""
    n = 0
    for fn, tree in _arboles():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and len(node.args) == 2):
                continue
            q, p = node.args
            if not (isinstance(q, ast.Constant) and isinstance(q.value, str)):
                continue
            if not isinstance(p, (ast.Tuple, ast.List)):
                continue
            if any(isinstance(e, ast.Starred) for e in p.elts):
                continue
            # Algunas llamadas van directas a psycopg2 y usan %s en vez de ?
            n_ph = (q.value.count("%s") if "%s" in q.value
                    else q.value.count("?"))
            n += 1
            if n_ph != len(p.elts):
                fallos.append(
                    f"{fn}:{node.lineno} {n_ph} placeholders "
                    f"vs {len(p.elts)} parámetros")
    return n


def clase3_campos(raw):
    """Campos leídos de una fila que su SELECT no devuelve."""
    def cols(sql):
        try:
            cur = raw.execute(re.sub(r"\?", "NULL", sql))
            return {d[0] for d in (cur.description or [])}
        except sqlite3.Error:
            return None

    n = 0
    for fn, tree in _arboles():
        funcs = [x for x in ast.walk(tree)
                 if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in funcs:
            asign = {}
            for node in ast.walk(func):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    continue
                v = node.value
                if not (isinstance(v, ast.Call)
                        and isinstance(v.func, ast.Attribute)
                        and v.func.attr in ("fetchone", "fetchall")):
                    continue
                inner = v.func.value
                if not (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "execute" and inner.args):
                    continue
                q = inner.args[0]
                if not (isinstance(q, ast.Constant)
                        and isinstance(q.value, str)):
                    continue
                sql = q.value
                if "{" in sql or "%s" in sql or "*" in sql:
                    continue
                if not sql.strip().upper().startswith("SELECT"):
                    continue
                cs = cols(sql)
                if cs:
                    asign[node.targets[0].id] = (cs, node.lineno)
            if not asign:
                continue
            for node in ast.walk(func):
                var = campo = None
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    var, campo = node.value.id, node.slice.value
                elif (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_wget" and len(node.args) == 2
                        and isinstance(node.args[0], ast.Name)
                        and isinstance(node.args[1], ast.Constant)):
                    var, campo = node.args[0].id, node.args[1].value
                if var in asign:
                    cs, ln = asign[var]
                    n += 1
                    if campo not in cs:
                        fallos.append(
                            f"{fn}:{node.lineno} lee {var}[{campo!r}] pero el "
                            f"SELECT de la línea {ln} no lo devuelve "
                            f"(fallo SILENCIOSO)")
    return n


def _campos_por_helper():
    """
    Funciones auxiliares que reciben una fila y leen campos de ella.
    Devuelve {nombre_funcion: {campos que necesita}}.

    Sin esto, un bug se escapa: la lectura ocurre DENTRO del helper
    (p. ej. _plan_salida(w)) mientras el SELECT está en quien lo llama.
    """
    helpers = {}
    for fn, tree in _arboles():
        for func in [x for x in ast.walk(tree)
                     if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not func.args.args:
                continue
            p0 = func.args.args[0].arg
            campos = set()
            for node in ast.walk(func):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_wget" and len(node.args) == 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == p0
                        and isinstance(node.args[1], ast.Constant)):
                    campos.add(node.args[1].value)
                elif (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == p0
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    campos.add(node.slice.value)
            if campos:
                helpers[func.name] = campos
    return helpers


def clase3b_helpers(raw):
    """Fila pasada a un helper que lee campos que el SELECT no trae."""
    def cols(sql):
        try:
            cur = raw.execute(re.sub(r"\?", "NULL", sql))
            return {d[0] for d in (cur.description or [])}
        except sqlite3.Error:
            return None

    helpers = _campos_por_helper()
    n = 0
    for fn, tree in _arboles():
        for func in [x for x in ast.walk(tree)
                     if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            asign = {}
            for node in ast.walk(func):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    continue
                v = node.value
                if not (isinstance(v, ast.Call)
                        and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "fetchone"):
                    continue
                inner = v.func.value
                if not (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "execute" and inner.args):
                    continue
                q = inner.args[0]
                if not (isinstance(q, ast.Constant)
                        and isinstance(q.value, str)):
                    continue
                sql = q.value
                if "{" in sql or "%s" in sql or "*" in sql:
                    continue
                if not sql.strip().upper().startswith("SELECT"):
                    continue
                cs = cols(sql)
                if cs:
                    asign[node.targets[0].id] = (cs, node.lineno)
            if not asign:
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in helpers and node.args
                        and isinstance(node.args[0], ast.Name)):
                    continue
                var = node.args[0].id
                if var not in asign:
                    continue
                cs, ln = asign[var]
                n += 1
                faltan = helpers[node.func.id] - cs
                if faltan:
                    fallos.append(
                        f"{fn}:{node.lineno} pasa {var} a {node.func.id}() "
                        f"que lee {sorted(faltan)}, pero el SELECT de la "
                        f"línea {ln} no lo devuelve (fallo SILENCIOSO)")
    return n


def _sin_tildes(x):
    return "".join(ch for ch in unicodedata.normalize("NFD", x)
                   if unicodedata.category(ch) != "Mn").lower()


def clase4_literales():
    """Valores escritos vs filtrados (desajustes por tilde/mayúscula)."""
    escritos, filtrados = {}, {}
    for fn in FILES:
        txt = open(os.path.join(RAIZ, fn)).read()
        for col in ("grade", "ai_class", "side", "status", "tier"):
            for m in re.finditer(rf"{col}\s*=\s*'([^']+)'", txt):
                escritos.setdefault(col, set()).add(m.group(1))
            for m in re.finditer(rf"{col}\s+IN\s*\(([^)]+)\)", txt, re.I):
                for v in re.findall(r"'([^']+)'", m.group(1)):
                    filtrados.setdefault(col, set()).add(v)
            for m in re.finditer(rf"{col}\s*==\s*['\"]([^'\"]+)", txt):
                filtrados.setdefault(col, set()).add(m.group(1))
    n = 0
    for col in set(escritos) | set(filtrados):
        for v in escritos.get(col, set()):
            for w in filtrados.get(col, set()):
                n += 1
                if v != w and _sin_tildes(v) == _sin_tildes(w):
                    fallos.append(
                        f"{col}: se escribe '{v}' pero se filtra por '{w}' "
                        f"(difieren en tildes/mayúsculas)")
    return n


def main():
    raw = _base()
    print("🔍 Auditoría del código\n")
    print(f"  clase 1 · SQL contra esquema real ....... {clase1_sql(raw)} consultas")
    print(f"  clase 2 · placeholders vs parámetros .... {clase2_params()} llamadas")
    print(f"  clase 3 · campos fuera de su SELECT ..... {clase3_campos(raw)} accesos")
    print(f"  clase 3b· campos exigidos por helpers ... {clase3b_helpers(raw)} llamadas")
    print(f"  clase 4 · literales escritos/filtrados .. {clase4_literales()} pares")
    print()
    if fallos:
        print(f"❌ {len(fallos)} hallazgo(s):\n")
        for f in fallos:
            print(f"   • {f}")
        return 1
    print("✅ Sin hallazgos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
