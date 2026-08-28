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
| Una excepción sin capturar | El supervisor del PC reinicia el bot |
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
- **PRODUCCIÓN (desde el 26/8/2026): el PC del dueño.**
  `C:\Users\black\wallet-edge-local` — clon real del repo, **SQLite**
  (`DB_PATH` en `bot_local.env`), bajo `supervisor.py` que hace
  auto-deploy desde GitHub cada 5 min (git manda: las ediciones locales
  se pisan solas). IA local Qwen vía LM Studio; nube de respaldo.
  - ⚠️ El PC descarga con **CRLF**: su `git status` sale siempre "sucio"
    y no significa nada. Comparar con `git diff --ignore-cr-at-eol` o
    hashes normalizados a LF.
  - `AUTO_CYCLE_HOURS=2` en su env (el default del código es 6).
- **Railway ya NO es producción.** El webhook de Helius que apuntaba
  allí lo desactivó Helius el 26/8 por 99,2% de fallos. Sin `PUBLIC_URL`
  en el PC, **LaserStream es la ÚNICA vía de ingesta**. Aun así, TODO SQL
  nuevo debe seguir funcionando en SQLite Y Postgres.
- **Subidas:** clonar el repo aparte (o subida web de GitHub). Nunca
  editar la carpeta del PC directamente: el supervisor la pisa.
- ~72 archivos `.py`. La red de seguridad son `auditoria.py` **y la
  suite `pruebas_ola18h.py` (300+ pruebas), que vive DENTRO del repo**
  porque los entornos de trabajo efímeros ya la perdieron dos veces.

### Antes de subir nada

```bash
python -m py_compile <archivos tocados>
python -m pyflakes <archivos tocados>
python auditoria.py                      # debe decir "Sin hallazgos"
python pruebas_ola18h.py                 # todas verdes (PRUEBAS_ESTRES_S=1 la acorta)
```

`auditoria.py` comprueba el SQL contra el esquema real, los placeholders
frente a los parámetros, y los campos que se leen fuera de su `SELECT`.
Ha cazado errores de verdad. **Pásalo siempre.** Y el método que ha
funcionado 13 olas seguidas: **mutación** (romper cada arreglo a mano y
comprobar que la suite lo caza) + **auditoría independiente repetida
hasta que no encuentre nada** + clon fresco byte a byte tras subir +
confirmar que el PC lo recogió y el bot sigue escribiendo señales.

---

## 3. Arquitectura mínima

- **Motor doble:** `db.py` habla SQLite *y* Postgres. `USE_PG` depende de
  que exista `DATABASE_URL`. **Cualquier SQL nuevo tiene que funcionar en
  los dos**, o llevar rama explícita. Postgres tiene `PERCENTILE_CONT`;
  SQLite no.
- **Migraciones:** columnas nuevas se añaden con `ALTER TABLE ... ADD COLUMN`
  idempotente al arrancar, en los dos motores. **No hay que tocar Railway
  a mano** para añadir una columna.
- **Entrada de datos:** **LaserStream** (única vía en el PC). El
  servidor Flask con `POST /helius` escucha SIEMPRE (verás el puerto
  abierto también en el PC), pero sin `PUBLIC_URL` Helius no registra
  el webhook: por ahí no entra nada.
- **Trabajos periódicos** (`telegram_bot.py`, final del archivo):

| Trabajo | Cada |
|---|---|
| `auto_cycle` | 6 h por defecto (`AUTO_CYCLE_HOURS`; **2 h en el PC**) |
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

## 5. El problema de memoria (agosto 2026 — era de Railway; el patrón sigue en el código)

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

## 6. El filtro de calidad y los mandos del dueño (olas 18-K/18-L, 27/8)

- **La estrella se GANA con tres puertas** (`filtro_calidad.py`):
  historial real (WR ≥60% en ≥10 posiciones cerradas/90d), estrategia
  copiable (retención ≥30 min, ≥8 tokens), y confirmación con señales
  medidas por el propio bot. `wallets.confirmada` = fase: solo las
  confirmadas alertan y se copian; las demás EN PRUEBA se miden en
  silencio y caen a los 14 días sin operar. Umbrales por env
  (`FILTRO_*`); `FILTRO_TRES_PUERTAS=0` lo apaga entero;
  `FILTRO_PROVISIONAL=1` (elegido por el dueño) confirma ya a quien
  pasa historial+copiabilidad mientras junta sus 5 primeras medidas.
- **Creadores de mercado** (>5 alternancias compra→venta al MISMO token
  en 30 días) pierden la estrella: `MM_VUELTAS_MAX` (0 apaga).
- **El "top N" es POSICIONAL sobre /top** — misma consulta y población
  en `top_wallets`, `_operativas` y `wallet_ident.posicion` (espejos
  obligatorios; las confirmadas ordenan primero). `top_addresses` tiene
  contrato de TRES estados: None = sin filtro / set vacío = nadie
  alerta. Consumidores SIEMPRE vía `db.en_top`.
- **Mandos**: `/filtro` (las puertas y quién las pasa), `/topalertas N`,
  `/reentrada H` (enfriamiento por token del paper; `/copiapura` ya no
  lo toca), `/copiapura on|off`, `/nota`.
- Regla del dueño, textual: **"quiero calidad no cantidad"** y **"no
  supongas nunca nada, siempre busca la evidencia"** — medir contra la
  base real ANTES de leer el código, y nunca proponer sin datos.

## 7. Cómo trabajar aquí

- **Explica cada decisión técnica en lenguaje sencillo.** El usuario no
  programa a nivel avanzado.
- **No inventes el contenido de archivos que no hayas leído.**
- **No supongas cifras ni estados:** verifica con `git log`, con la
  suite, o midiendo contra la base real del PC.
- **Nada de `git add` / `commit` / `push` sin autorización explícita en la
  sesión actual.**
- Cambios pequeños y verificables, de uno en uno.
- Si algo no se puede hacer bien, dilo en vez de simularlo.
