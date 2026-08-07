# Bot de billeteras — contexto operativo

Lee esto entero antes de proponer cualquier cambio.

---

## 0. Lo primero, porque condiciona todo lo demás

**Este bot NO mueve dinero. No puede.**

No hay SDK de Solana, ni `Keypair`, ni clave privada, ni firma, ni envío de
transacciones. Las dependencias completas son `requests`,
`python-telegram-bot`, `flask`, `psycopg2-binary`, `pillow` y
`websocket-client`. La capacidad de operar no está desactivada: **no está
instalada**.

El *paper trading* es una **simulación** que vive en una tabla de la base de
datos. Anota qué habría pasado si copiaras las señales. Nadie compra nada.

### Qué se puede romper y qué no

| Si algo falla | Consecuencia real |
|---|---|
| Un cálculo mal | Un número raro en un mensaje de Telegram |
| Una excepción sin capturar | El bot se reinicia en Railway |
| Un despliegue malo | Se revierte y ya está |
| **Borrar `appearances` o `signals`** | **Se pierde el histórico, que no se recupera** |

Lo único verdaderamente irreversible es el **histórico acumulado**. Es la
investigación de semanas: billeteras puntuadas, apariciones, señales
medidas. Eso sí merece cuidado. El resto se corrige y se vuelve a desplegar.

**No trates este bot como si manejara dinero.** Hacerlo lleva a recomendar
parches tímidos en vez del arreglo bueno.

---

## 1. Qué hace

Un sistema de **observación e investigación** sobre Solana:

1. **Descubre** tokens que ya subieron (DexScreener, GeckoTerminal)
2. **Averigua quién los compró temprano** (Helius, solo lectura)
3. **Puntúa** esas billeteras y le pone ⭐ a las buenas
4. **Avisa por Telegram** cuando una ⭐ compra o vende
5. **Simula** en la tabla `paper_trades` qué habría pasado copiándolas

Todo lo que sale hacia fuera son consultas a Helius, al proveedor de IA, y
mensajes a Telegram.

---

## 2. Dónde vive

- **Repositorio:** https://github.com/pruebastest85-cyber/proyecto-bot-telegram
- **Producción:** Railway, proyecto `ravishing-youthfulness`, servicio `worker`
  - `Procfile`: `python migrate_to_pg.py && python telegram_bot.py`
  - Base: servicio **Postgres** aparte (con `DATABASE_URL`)
  - Región Southeast Asia · Python 3.13 · 1 réplica
- **Copia local:** `C:\Users\black\OpenHandsTest\project\bot\`
  - ⚠️ **El `.git` de esa carpeta está VACÍO** — no es un clon de verdad.
    No tiene remoto ni `HEAD`. Para subir hay que clonar el repo aparte,
    o usar la subida web de GitHub.
- 60 archivos `.py`, ~14.200 líneas. Sin tests formales; la red de
  seguridad es `auditoria.py`.

### Antes de subir nada

```bash
python -m py_compile <archivos tocados>
python auditoria.py          # debe decir "Sin hallazgos"
```

`auditoria.py` comprueba el SQL contra el esquema real, los placeholders
frente a los parámetros, y los campos que se leen fuera de su `SELECT`.
Ha cazado errores de verdad. **Pásalo siempre.**

---

## 3. Arquitectura mínima

- **Motor doble:** `db.py` habla SQLite *y* Postgres. `USE_PG` depende de
  que exista `DATABASE_URL`. **Cualquier SQL nuevo tiene que funcionar en
  los dos**, o llevar rama explícita. Postgres tiene `PERCENTILE_CONT`;
  SQLite no.
- **Migraciones:** columnas nuevas se añaden con `ALTER TABLE ... ADD COLUMN`
  idempotente al arrancar, en los dos motores. **No hay que tocar Railway
  a mano** para añadir una columna.
- **Entrada de datos:** webhook de Helius (`POST /helius`, Flask) y
  LaserStream como respaldo.
- **Trabajos periódicos** (`telegram_bot.py`, final del archivo):

| Trabajo | Cada |
|---|---|
| `auto_cycle` | 6 h (`AUTO_CYCLE_HOURS`) |
| `track_outcomes` | 15 min |
| `paper_trading` | 15 min |
| `predictions_eval` | **10 min** |
| `sync_webhook` | 30 min |
| `watchdog` | 1 h |
| `salud` | 6 h |
| `hypotheses` | 12 h |
| `db_backup`, `performance_review`, `daily_summary` | 24 h |
| `weekly_learning` | 7 días |

---

## 4. Trampas conocidas — no volver a caer

**Los logs rojos de Railway mienten.** Cientos de líneas marcadas como error
son `POST /helius 200`, o sea peticiones correctas. Salen en rojo porque el
servidor escribe su registro de accesos en `stderr` y Railway pinta de rojo
todo `stderr`. **No busques fallos ahí.**

**El aviso de verdad está en el badge rojo de la tarjeta del servicio.**
Es un tooltip de Radix: hay que pasarle eventos de *puntero*, no de ratón,
para que se abra.

**Notación científica en los precios.** Los memecoins están por debajo de
0.0001, donde `:.6g` cambia a `8.342e-05`. Usa `_fmt_price` de
`card_image.py`. Ya pasó una vez.

**El grafo de influencia es cuadrático.** Ver la sección siguiente.

---

## 5. El problema de memoria (agosto 2026)

**Síntoma medido:** el servicio muere por `Out of memory` (límite 8 GB). La
memoria sube sostenida hasta ~7,5 GB, Railway lo mata, reinicia sobre 2 GB y
vuelve a subir. Diente de sierra clásico.

**Causa, en `influence.py`:** para cada token recorre **todas las parejas**
de compradores. Con 100 compradores son 4.950 parejas; con 500, son 124.750.
De un solo token. Y guarda por pareja un diccionario con una lista `gaps`
que crece sin tope.

Agravantes:

- El resultado se cachea en `_CACHE["g"]` y no se suelta nunca.
- `predictions_job` corre cada **10 min** y llama a `graph()`; el TTL es de
  **300 s**, así que **siempre** reconstruye.
- `_CACHE["g"] = g` se asigna *después* de construir el nuevo → durante unos
  segundos hay **dos grafos enteros en RAM**.
- `hidden_leaders()` llama a `influence(addr)` dentro de un bucle sobre
  todas las billeteras, y cada llamada recorre **todas** las aristas.

**El mismo patrón por parejas está en `entity_resolution.py`** (línea ~66,
tres listas por pareja) y algo parecido en `alpha.py` y `clusters.py`.

**Segundo foco, menor:** `backup.py` hace `SELECT * FROM <tabla>` +
`.fetchall()` de toda la base y la vuelca a diccionarios de Python antes de
escribir nada. Pico diario.

### Cómo NO arreglarlo

- **Recortar compradores por token**: pierde información. Solo vale como
  parche de emergencia, nunca como plan.
- **Podar `appearances`**: es el histórico. Último recurso.
- **Subir a Pro por más RAM**: el crecimiento es cuadrático; volverías a
  chocar más tarde y pagando.

### Cómo sí

Que el conteo de parejas lo haga **Postgres** con un `JOIN` agrupado, y que
solo devuelva las parejas con `shared >= MIN_SHARED` (las de menos ya las
descarta `_weight`, o sea que no se pierde nada que se use).

---

## 6. Cómo trabajar aquí

- **Explica cada decisión técnica en lenguaje sencillo.** El usuario no
  programa a nivel avanzado.
- **No inventes el contenido de archivos que no hayas leído.**
- **No supongas cifras ni estados:** verifica con `git log`, con la suite,
  o mirando Railway.
- **Nada de `git add` / `commit` / `push` sin autorización explícita en la
  sesión actual.**
- Cambios pequeños y verificables, de uno en uno.
- Si algo no se puede hacer bien, dilo en vez de simularlo.
