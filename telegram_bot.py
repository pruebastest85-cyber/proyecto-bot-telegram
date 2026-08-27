"""
Bot de Telegram — Panel de control del pipeline de billeteras.
v5: HUB de navegación con menú de botones inline (se edita en el mismo
mensaje), secciones agrupadas, botones « Inicio / Cancelar y flujo
"pídeme la dirección" para los comandos que necesitan un <address>.
Mantiene TODO lo de v4 (alias IA, PnL, /top con botones, chat libre,
jobs automáticos, Mini App /app).

Variables de entorno:
  TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, HELIUS_API_KEY, DB_PATH
  AUTO_CYCLE_HOURS (opcional, default 6), PUBLIC_URL (para /app)
"""

import asyncio
import os
import re
import threading
import time as _t

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      WebAppInfo, BotCommand)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import config
from db import get_conn, wallet_evidence
from discovery import run_discovery
from wallet_analyzer import run_analysis
from wallet_profiler import profile_wallet, format_profile
from wallet_admin import (discard_wallet, restore_wallet, build_top_message)
from realtime import start_webhook_server, sync_helius_webhook

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_ADMIN_ID_RAW = os.getenv("TELEGRAM_ADMIN_ID")
if not _ADMIN_ID_RAW:
    raise RuntimeError(
        "Falta TELEGRAM_ADMIN_ID: sin este ID cualquier usuario puede "
        "ejecutar comandos de administrador. Configúralo como variable de entorno."
    )
try:
    ADMIN_ID = int(_ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        f"TELEGRAM_ADMIN_ID no es un entero válido: {_ADMIN_ID_RAW!r}"
    )
# Un 0 explícito reabriría el bypass: el decorador solo_admin comprueba
# `if ADMIN_ID and ...`, y 0 es falsy, así que no bloquearía a nadie.
if ADMIN_ID <= 0:
    raise RuntimeError(
        f"TELEGRAM_ADMIN_ID debe ser un ID de Telegram positivo, no "
        f"{ADMIN_ID}. Con 0 el filtro de administrador queda desactivado."
    )
AUTO_CYCLE_HOURS = float(os.getenv("AUTO_CYCLE_HOURS", "6"))

# Evita que el ciclo automático y un comando manual corran a la vez
cycle_lock = threading.Lock()

# Acciones del agente pendientes de confirmación (una por usuario)
PENDING_ACTIONS: dict[int, dict] = {}
# (Ola 17-B) Propuestas YA consumidas: {(user_id, tok): (estado, ts)}.
# `PENDING_ACTIONS.pop` vacia el hueco en la PRIMERA pulsacion; con
# concurrent_updates(8) el segundo toque del mismo boton (habitual en
# movil, y Telegram reintenta callbacks) encontraba None y editaba el
# mensaje con "⌛ Propuesta expirada (el bot se reinicio). Vuelve a
# pedirla" — encima de una accion que SI se estaba ejecutando, e
# invitando a repetirla. Con esto se distingue "nunca existio" de "ya
# la consumi yo mismo hace un momento".
ACCIONES_CONSUMIDAS: dict[tuple, tuple] = {}
_CONSUMIDAS_TTL = 900


def _marcar_consumida(uid: int, tok: str, estado: str):
    import time as _tt
    ahora = _tt.time()
    for k, v in list(ACCIONES_CONSUMIDAS.items()):
        if ahora - v[1] > _CONSUMIDAS_TTL:
            ACCIONES_CONSUMIDAS.pop(k, None)
    ACCIONES_CONSUMIDAS[(uid, tok)] = (estado, ahora)

# Usuarios a los que el hub les pidió un dato (address o pregunta).
# user_id -> nombre del comando ("perfil", "ficha", "preguntar"…)
AWAITING: dict[int, str] = {}

# Un contrato/mint de Solana pegado directo (base58, 32-44 chars)
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Tareas en segundo plano vivas (sin esta referencia el GC puede matarlas)
_BG_TASKS: set = set()


# ── (Ola 18-E) Texto ajeno dentro de un mensaje en Markdown ──────────
# El simbolo de un token lo elige quien crea el token, y viaja hasta los
# mensajes. Medido en la base del dueño: **439 tokens** tienen `*`, `_`,
# `[` o backtick en el simbolo. Con uno de esos, Telegram rechaza el
# mensaje entero; el reintento en texto plano lo salva, pero llega sin
# formato — y si ademas es largo, no llega.
_MD_FUERA = {"*": "", "_": " ", "`": "", "[": "(", "]": ")"}

# Tope de Telegram para un mensaje de texto.
TG_MAX_CHARS = 4096


def _md_escapar(txt) -> str:
    """Deja el texto que NO escribimos nosotros (simbolo de token, razon
    de la IA, alias) sin nada que Telegram lea como formato.

    Se QUITAN los caracteres en vez de escaparlos con `\\`, que es lo que
    ya hace el resto del proyecto (digest, /salud, rendimiento,
    wallet_links): el Markdown legacy de Telegram no des-escapa la barra
    invertida en todos los contextos y acaban viendose barras sueltas en
    el mensaje."""
    s = str(txt if txt is not None else "")
    for c, r in _MD_FUERA.items():
        s = s.replace(c, r)
    return s


def _largo_tg(text: str) -> int:
    """Longitud como la cuenta Telegram: unidades UTF-16, no caracteres.
    Cada emoji fuera del BMP (🎯 🚀 ⭐ 💵…) cuenta DOS, y estos mensajes
    van llenos de ellos: medir con `len()` deja pasar mensajes de ~4.000
    caracteres que Telegram rechaza igual."""
    return len(text.encode("utf-16-le")) // 2


def _cerrar_markdown(cuerpo: str) -> str:
    """Quita los marcadores de Markdown que el recorte dejo sin cerrar.

    (Ola 18-H) Recortar por el medio puede dejar un `*` abierto, y
    entonces Telegram devuelve 400: el mensaje se salva por el reintento
    en texto plano de `_send_md`, pero llega con todos los asteriscos y
    guiones bajos a la vista — justo lo que el saneado de simbolos de la
    Ola 18-E venia a evitar.

    (2ª vuelta) La primera version contaba marcadores y miraba la
    paridad, y eso NO es como lee Telegram. Dentro de un ``code span`` los
    `_` son literales, asi que borrar el backtick huerfano los convertia
    en delimitadores de verdad — y ya no se volvian a contar. Medido con
    el texto real de `/metricas` (lleva `bot_local.env`, `PRED_ALPHA_META`
    y una linea en cursiva): **547 de 558 longitudes de corte** salian
    rotas, y con enlaces `[texto](url)`, que la paridad no mira siquiera,
    473 de 558.

    Ahora se recorre de izquierda a derecha como lo hace Telegram: al
    encontrar un marcador se busca su pareja, y lo que hay entre medias
    se salta entero (por eso los `_` de dentro de un backtick ya no
    cuentan). Solo se borran los que se quedan sin pareja hasta el final.
    """
    huerfanos = []
    i, n = 0, len(cuerpo)
    while i < n:
        c = cuerpo[i]
        # (3ª vuelta) Los tres backticks son UN delimitador para Telegram
        # (bloque de codigo), no tres sueltos. Si se emparejan de dos en
        # dos, una racha impar deja uno colgando — y ademas el borrado
        # puede CREAR una racha de tres que antes no existia.
        if c == "`" and cuerpo[i:i + 3] == "```":
            j = cuerpo.find("```", i + 3)
            if j < 0:
                huerfanos += [i, i + 1, i + 2]
                i += 3
            else:
                i = j + 3
            continue
        # (4ª vuelta) Telegram des-escapa `\_ \* \` \[`: esos dos
        # caracteres son texto, no marcador. Se saltan juntos.
        if c == "\\" and cuerpo[i + 1:i + 2] in ("_", "*", "`", "["):
            i += 2
            continue
        if c in ("`", "*", "_"):
            j = cuerpo.find(c, i + 1)
            if j < 0:
                huerfanos.append(i)
                i += 1
            else:
                i = j + 1
            continue
        if c == "[":
            # (3ª vuelta) Telegram cierra el `[` en el PRIMER `]`, no en
            # el primer `](`. Buscando `](` , un `[` suelto se ataba al
            # enlace que viniera despues y se saltaba TODO lo de en medio:
            # los marcadores de esa zona dejaban de contarse, su pareja de
            # fuera parecia huerfana y se borraba un marcador que SI estaba
            # cerrado. Medido con un fuzz de 200.000 cadenas: 11 mensajes
            # que Telegram aceptaba pasaban a rechazarse. Caso real:
            #   "El pool [1] estaba *fino: mira [Dex](https://d.io/x) y*"
            # (4ª vuelta) Y con las reglas EXACTAS de Telegram, leidas de
            # su propio codigo (`parse_markdown` de tdlib), no de lo que
            # parece razonable. Telegram ACEPTA `[texto]` sin URL (usa el
            # propio texto como enlace) y `[texto](url` sin cerrar. Exigir
            # `](url)` hacia que se borrara el `[` de cosas tan normales
            # como "el pool [A] quedo fino": medido, 3.355 de 40.000
            # cadenas validas salian modificadas, y en el corpus de las
            # pruebas 86 de 654 cortes perdian algun corchete. Un `[` solo
            # sobra cuando NO hay ningun `]` detras.
            j = cuerpo.find("]", i + 1)
            if j < 0:
                huerfanos.append(i)
                i += 1
            elif j == i + 1:
                # `[]`: entidad vacia. Telegram no lee la URL, asi que el
                # `(` de detras es texto llano y hay que seguir mirandolo.
                i = j + 1
            elif cuerpo[j + 1:j + 2] != "(":
                i = j + 1
            else:
                k = cuerpo.find(")", j + 2)
                if k < 0:
                    # (5ª vuelta) `[texto](url` sin cerrar el parentesis
                    # es VALIDO para Telegram… porque se traga todo lo que
                    # venga detras. Y detras siempre va algo: `_recortar_tg`
                    # pega "…\n\n_(mensaje recortado: N caracteres)_". Ese
                    # barrido se comia el aviso hasta el `)` de
                    # "caracteres)" y dejaba el `_` final huerfano → 400.
                    # Medido con el corpus de enlaces de las pruebas: 205
                    # de 572 cortes salian rechazados. Asi que aqui el `[`
                    # SI sobra: no es que el mensaje sea invalido, es que
                    # contamina lo que se le pegue detras.
                    huerfanos.append(i)
                    i += 1
                else:
                    i = k + 1
            continue
        i += 1
    if not huerfanos:
        return cuerpo
    for pos in reversed(huerfanos):
        cuerpo = cuerpo[:pos] + cuerpo[pos + 1:]
    # (3ª vuelta) Se repite: borrar caracteres puede juntar backticks que
    # antes estaban separados y formar una racha nueva. Converge siempre
    # porque la cadena solo puede encoger.
    return _cerrar_markdown(cuerpo)


def _recortar_tg(text: str, tope: int | None = None) -> str:
    """Un mensaje mas largo que el tope de Telegram NO se envia — ni en
    Markdown ni en texto plano. Medido: la evidencia de la billetera mas
    activa del dueño ocupa 6.585 caracteres, o sea que hoy ese mensaje se
    pierde entero. Mejor recortado y diciendolo que perdido en silencio.

    (Ola 18-H) `tope` es solo para poder probarlo con textos cortos; en
    produccion se usa siempre el de Telegram.
    """
    tope = TG_MAX_CHARS if tope is None else int(tope)
    largo = _largo_tg(text)
    if largo <= tope:
        return text
    aviso = f"\n\n_(mensaje recortado: {largo} caracteres)_"
    # Se recorta a ojo y se comprueba de verdad: cortar por caracteres no
    # garantiza el largo en UTF-16, asi que se ajusta hasta que entra.
    cuerpo = text[:max(0, tope - 120)]
    while _largo_tg(cuerpo) + _largo_tg(aviso) + 1 > tope and cuerpo:
        cuerpo = cuerpo[:-40] or ""
    cuerpo = _cerrar_markdown(cuerpo.rstrip())
    return cuerpo.rstrip() + "…" + aviso


async def _send_md(chat, text, **kw):
    """Envía en Markdown; si Telegram lo rechaza (símbolos raros del token),
    reintenta en texto plano para NO perder el mensaje en silencio.
    Mismo criterio que realtime.tg_send.

    (Ola 16) Acepta Chat o Message: pasar `update.message` por error dejaba
    /radar, /postmortem y /salidas MUDOS — AttributeError capturado por el
    propio except, dos líneas de "Markdown rechazado" y silencio total.

    (Ola 18-E) Recorta al tope de Telegram (si no, el mensaje no se envia
    de ninguna forma) y, cuando ni el texto plano entra, lo apunta en
    `/errores`: antes solo quedaba un `print` en una ventana que nadie
    mira, igual que el fallo que la Ola 17-L cerro en `tg_send`."""
    chat = getattr(chat, "chat", chat)
    text = _recortar_tg(text)
    try:
        return await chat.send_message(text, parse_mode="Markdown", **kw)
    except Exception as e:
        print(f"· Markdown rechazado ({e}); reintento en texto plano")
        try:
            return await chat.send_message(text, **kw)
        except Exception as e2:
            print(f"· No se pudo enviar el mensaje: {e2}")
            try:
                from errores import record as _rec_md
                await asyncio.to_thread(
                    _rec_md, "telegram_send_md", e2,
                    f"{_largo_tg(text)} unidades (tope {TG_MAX_CHARS}); "
                    f"Markdown fallo con: "
                    f"{str(e)[:120]}")
            except Exception:
                pass
            return None


def _token_keyboard(url, mint):
    """Botones bajo la ficha de token: DexScreener + 👍/👎 para aprender."""
    filas = []
    if url:
        filas.append([InlineKeyboardButton("📈 Ver en DexScreener", url=url)])
    filas.append([
        InlineKeyboardButton("👍 Buena", callback_data=f"tk:up:{mint}"),
        InlineKeyboardButton("👎 Mala", callback_data=f"tk:dn:{mint}")])
    return InlineKeyboardMarkup(filas)


# ─────────────────────────── HUB / MENÚ ────────────────────────────

def hub_text() -> str:
    return (
        "🔍 *Wallet Discovery Bot*\n"
        f"⚙️ Ciclo automático cada {AUTO_CYCLE_HOURS:g} h  ·  "
        "💬 escríbeme normal para chatear con la IA\n\n"
        "Elige una sección 👇"
    )


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _row_inicio() -> list:
    return [_btn("« Inicio", "h:home")]


def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("⚡ Acciones", "h:acciones"), _btn("🏆 Billeteras", "h:wallets")],
        [_btn("🔎 Analizar wallet", "h:consultar"), _btn("🤖 IA & Chat", "h:ia")],
        [_btn("📊 Estado", "h:run:status"), _btn("📱 Panel visual", "h:app")],
        [_btn("ℹ️ Todos los comandos", "h:help")],
    ])


def kb_acciones() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔄 Ciclo completo", "h:run:ciclo")],
        [_btn("🔍 Descubrir tokens", "h:run:descubrir")],
        [_btn("🧮 Analizar compradores", "h:run:analizar")],
        [_btn("🧪 Paper trading", "pap:ver")],
        _row_inicio(),
    ])


def kb_wallets() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🏆 Top 10", "h:run:top10"), _btn("🏆 Top 20", "h:run:top20")],
        [_btn("📡 Últimas señales", "h:run:senales")],
        _row_inicio(),
    ])


def kb_consultar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔬 Perfil", "h:ask:perfil"), _btn("🧮 Ficha / Score", "h:ask:ficha")],
        [_btn("🧠 Veredicto IA", "h:ask:ia"), _btn("📋 Evidencia", "h:ask:evidencia")],
        [_btn("❌ Descartar", "h:ask:descartar"), _btn("⭐ Rastrear", "h:ask:rastrear")],
        [_btn("📊 Rendimiento", "h:run:rendimiento"), _btn("🧪 Backtest", "h:run:backtest")],
        [_btn("💰 Saldos", "h:run:saldos"), _btn("🔗 Hermanas", "h:run:hermanas")],
        _row_inicio(),
    ])


def kb_ia() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("💬 Preguntar a la IA", "h:ask:preguntar")],
        _row_inicio(),
    ])


def kb_solo_inicio() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([_row_inicio()])


def kb_cancelar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("« Cancelar", "h:home")]])


def kb_paper() -> InlineKeyboardMarkup:
    """Botones bajo el resumen de /paper."""
    return InlineKeyboardMarkup([
        [_btn("🔄 Actualizar", "pap:ver"),
         _btn("🗑 Reiniciar", "pap:pedir")],
    ])


def kb_paper_confirmar(abiertas: int, cerradas: int) -> InlineKeyboardMarkup:
    """Segundo paso: borrar el historial es irreversible, así que el
    primer botón solo pregunta y este es el que ejecuta."""
    return InlineKeyboardMarkup([
        [_btn(f"⚠️ Sí, borrar {abiertas + cerradas}", "pap:reset")],
        [_btn("« No, dejarlo como está", "pap:ver")],
    ])


SECCIONES = {
    "acciones": ("⚡ *Acciones del pipeline*\n\nEjecuta tareas ahora mismo:",
                 kb_acciones),
    "wallets": ("🏆 *Billeteras*\n\nMira el ranking y las señales:", kb_wallets),
    "consultar": ("🔎 *Analizar una wallet*\n\nElige qué quieres ver; luego te "
                  "pediré la dirección:", kb_consultar),
    "ia": ("🤖 *IA & Chat*\n\nPregúntale a la IA sobre tu base, o simplemente "
           "escríbeme un mensaje normal y el agente responde o propone "
           "acciones:", kb_ia),
}

ASK_PROMPTS = {
    "perfil": "🔬 *Perfil de billetera*\nEnvíame la *dirección* de la wallet a investigar:",
    "ficha": "🧮 *Ficha / Wallet Score*\nEnvíame la *dirección* de la wallet:",
    "ia": "🧠 *Veredicto IA*\nEnvíame la *dirección* de la wallet:",
    "evidencia": "📋 *Evidencia*\nEnvíame la *dirección* de la wallet:",
    "descartar": "❌ *Descartar billetera*\nEnvíame la *dirección* a dejar de rastrear:",
    "rastrear": "⭐ *Rastrear billetera*\nEnvíame la *dirección* a revertir el descarte:",
    "preguntar": ("💬 *Preguntar a la IA*\nEscríbeme tu pregunta sobre la base "
                  "(top, señales, ROI, quién acumula…):"),
}

HELP_TEXT = (
    "ℹ️ *Todos los comandos*\n\n"
    "/ciclo — descubrimiento + análisis ahora\n"
    "/descubrir — buscar tokens ganadores\n"
    "/analizar — analizar compradores tempranos\n"
    "/top [n] — mejores billeteras (10/20/30) con ❌ para descartar\n"
    "/descartar <address> — dejar de rastrear una billetera\n"
    "/rastrear <address> — revertir un descarte\n"
    "/evidencia <address> — el porqué de una billetera\n"
    "/perfil <address> — investigar una billetera a fondo\n"
    "/ficha <address> — Wallet Score 0-100 con ROI y riesgo\n"
    "/preguntar <texto> — pregúntale a la IA sobre tu base\n"
    "/ia <address> — veredicto de la IA sobre una billetera\n"
    "/senales — últimas señales en tiempo real\n"
    "/status — estado de la base de datos\n"
    "/app — panel visual (Mini App)\n\n"
    "💬 También puedes escribirme normal (sin /) para chatear con el agente."
)


# ──────────────────── LÓGICA REUTILIZABLE (bloqueante) ─────────────────

def run_full_cycle() -> str:
    """Ejecuta descubrimiento + análisis. Devuelve resumen en texto."""
    if not cycle_lock.acquire(blocking=False):
        return "⏳ Ya hay un ciclo en curso; este intento se omitió."
    try:
        saved = run_discovery()
        run_analysis()
        conn = get_conn()
        wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
        tracked = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
        conn.close()
        hook_msg = sync_helius_webhook()
        print(f"📡 {hook_msg}")
        return (f"✅ Ciclo terminado.\n"
                f"Ganadores nuevos: {saved}\n"
                f"Billeteras totales: {wallets}\n"
                f"Rastreadas ⭐: {tracked}\n"
                f"📡 {hook_msg}")
    finally:
        cycle_lock.release()


def _status_text() -> str:
    conn = get_conn()
    try:
        tokens = conn.execute("SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"]
        pend = conn.execute(
            "SELECT COUNT(*) c FROM winning_tokens WHERE analyzed=0").fetchone()["c"]
        wallets = conn.execute("SELECT COUNT(*) c FROM wallets").fetchone()["c"]
        tracked = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
        descartadas = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_bot=1").fetchone()["c"]
        from db import get_setting
        umbral = get_setting(conn, "min_signal_score", "0")
        # (Ola 8, 21/8) Antes se mostraban los contadores funnel_profiled/
        # funnel_promoted, que suman EVALUACIONES de por vida (la misma
        # billetera cuenta N veces al re-evaluarse): decian "484/6800" con
        # 72 seguidas reales. Ahora: billeteras DISTINTAS, en vivo.
        prof = conn.execute(
            "SELECT COUNT(*) c FROM wallets "
            "WHERE ai_class IS NOT NULL").fetchone()["c"]
        prom = conn.execute(
            "SELECT COUNT(*) c FROM wallets "
            "WHERE ai_follow=1").fetchone()["c"]
        try:
            from api_usage import usage_line, flush as _api_flush
            _api_flush()
            apis = usage_line(conn)
        except Exception:
            apis = ""
    finally:
        conn.close()
    return (
        f"📊 *Estado del sistema*\n\n"
        f"⚙️ Ciclo automático: cada {AUTO_CYCLE_HOURS:g} h\n"
        f"🎯 Umbral de señal: {float(umbral or 0):.0f}/100\n"
        f"Tokens ganadores: {tokens} ({pend} pendientes)\n"
        f"Billeteras registradas: {wallets}\n"
        f"Billeteras rastreadas ⭐: {tracked}\n"
        f"Descartadas/bots ❌: {descartadas}"
        + (f"\n📈 Embudo: {prof} evaluadas → {prom} seguidas "
           f"({100 * prom / prof:.0f}%)" if prof else "")
        + (f"\n{apis}" if apis else ""))


def _senales_text() -> str:
    conn = get_conn()
    # (Ola 8, 21/8) El feed guarda señales de ⭐ Y de candidatas en
    # observación (liga de ascenso): se marca cuál es cuál en vez de
    # presentarlo todo como si fuera de ⭐.
    rows = conn.execute(
        "SELECT s.*, COALESCE(w.is_tracked, 0) es_estrella FROM signals s "
        "LEFT JOIN wallets w ON w.address = s.wallet "
        "ORDER BY s.ts DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        return ("📡 *Últimas señales*\n\n"
                "_Aún no hay señales. Llegarán cuando una billetera ⭐ "
                "compre o venda algo._")
    lines = ["📡 *Últimas señales*  _(⭐ rastreada · 👁 en observación)_",
             "━━━━━━━━━━━━━━", ""]
    for s in rows:
        # (Ola 8) minutos enteros con acarreo: 59,6 min ya no es "60 min"
        mins = int(round((_t.time() - s["ts"]) / 60))
        cuando = (f"hace {mins/60:.1f}h" if mins >= 60
                  else f"hace {mins} min")
        try:
            side = s["side"] or "compra"
        except (KeyError, IndexError):
            side = "compra"
        emoji = "🟢" if side == "compra" else "🔴"
        verbo = "compra" if side == "compra" else "venta"
        try:
            simbolo = s["symbol"] or (s["mint"][:10] + "…")
        except (KeyError, IndexError):
            simbolo = s["mint"][:10] + "…"
        simbolo = (str(simbolo).replace("*", "").replace("_", " ")
                   .replace("`", "").replace("[", ""))
        res = ""
        try:
            partes = []
            if s["chg_1h"] is not None:
                partes.append(f"1h {s['chg_1h']:+.0f}%")
            if s["chg_24h"] is not None:
                partes.append(f"24h {s['chg_24h']:+.0f}%")
            if partes:
                res = "\n    📈 " + "  ·  ".join(partes)
        except (KeyError, IndexError):
            pass
        try:
            marca = "⭐" if s["es_estrella"] else "👁"
        except (KeyError, IndexError):
            marca = ""
        lines.append(
            f"{emoji} *{simbolo}*  ·  {verbo}  ·  "
            f"{(s['sol'] or 0):.2f} SOL"
            f"  {marca}\n    🕒 {cuando}{res}\n")
    return "\n".join(lines).rstrip()


# Cuantas apariciones se muestran en /evidencia. Medido en la base del
# dueño: la billetera mas activa tiene 65 apariciones y su texto ocupa
# 6.585 caracteres — por encima del tope de 4.096 de Telegram, o sea que
# ese mensaje no se enviaba NI en Markdown ni en texto plano. 30
# billeteras pasan de 20 apariciones.
EVIDENCIA_MAX = 25


def _evidencia_text(address: str) -> str:
    conn = get_conn()
    rows = wallet_evidence(conn, address)
    conn.close()
    if not rows:
        return "Sin registros para esa dirección."
    total = len(rows)
    # Las mas recientes primero: `wallet_evidence` ordena por buy_time
    # ascendente, y lo interesante de una billetera es lo ultimo que hizo.
    mostradas = rows[-EVIDENCIA_MAX:][::-1]
    lines = [f"📋 *Evidencia de* `{address[:20]}…`"
             + (f"  ·  {total} apariciones" if total > 1 else "") + "\n"]
    for ev in mostradas:
        # `reason` lleva dentro el SIMBOLO del token, que lo elige quien
        # crea el token: sin limpiarlo, un `*` o un `_` rompe el Markdown
        # del mensaje entero (439 tokens de la base tienen alguno).
        lines.append(f"• {_md_escapar(ev['reason'])}\n")
    if total > len(mostradas):
        lines.append(f"_…y {total - len(mostradas)} apariciones más "
                     f"antiguas que no caben en un mensaje._\n")
    lines.append(f"\n🔗 Verificar: gmgn.ai/sol/address/{address}")
    return "\n".join(lines)


def _ficha_text(address: str):
    from wallet_score import compute_score, format_ficha
    from signal_tracker import wallet_track_record, format_track_record
    p = profile_wallet(address)
    if not p["tx_sampled"]:
        # (Ola 18-D) None = "no hay nada"; el texto explica si en realidad
        # es "no pude bajarlo", que no es lo mismo.
        if p.get("historial_entero") is False:
            return ("⚠️ No pude descargar el historial (Helius se cortó a "
                    "mitad). Inténtalo de nuevo en un rato.")
        return None
    conn = get_conn()
    track = wallet_track_record(conn, address)
    row = conn.execute("SELECT alias FROM wallets WHERE address=?",
                       (address,)).fetchone()
    conn.close()
    s = compute_score(p, track)
    alias = row["alias"] if row and row["alias"] else None
    ficha = format_ficha(address, s, alias, format_track_record(track))
    # Saldo actual on-chain
    try:
        import requests as _rq
        resp = _rq.post(config.HELIUS_RPC,
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": "getBalance", "params": [address]},
                        timeout=15)
        sol = resp.json()["result"]["value"] / 1e9
        ficha += f"\n💰 Saldo actual: *{sol:,.2f} SOL*"
    except Exception:
        pass
    return ficha


def _ia_text(address: str) -> str:
    from ai_analyst import ai_verdict
    p = profile_wallet(address)
    if not p["tx_sampled"]:
        # (Ola 18-D) Distinguir "no tiene actividad" de "no pude bajarla".
        if p.get("historial_entero") is False:
            return ("⚠️ No pude descargar el historial (Helius se cortó a "
                    "mitad). No es que la billetera esté vacía: no tengo "
                    "el dato. Inténtalo de nuevo en un rato.")
        return "Sin transacciones para esa dirección."
    conn = get_conn()
    ev = conn.execute(
        "SELECT reason FROM appearances WHERE wallet=? LIMIT 6",
        (address,)).fetchall()
    conn.close()
    v = ai_verdict(p, [e["reason"] for e in ev])
    if not v:
        return "La IA no devolvió veredicto (¿ANTHROPIC_API_KEY configurada?)."
    icono = "✅ SEGUIR" if v["seguir"] else "❌ DESCARTAR"
    alias_txt = f"Alias: 👤 *{v['alias']}*\n" if v.get("alias") else ""
    return (f"🧠 *Veredicto IA para* `{address[:16]}…`\n\n"
            f"{alias_txt}"
            f"Clasificación: *{v['clasificacion'].upper()}*\n"
            f"Recomendación: {icono}\n"
            f"Confianza: {v.get('confianza', '?')}%\n\n"
            f"_{v.get('razon', '')}_")


def app_keyboard():
    """Teclado con el botón de la Mini App, o None si falta PUBLIC_URL."""
    public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    public_url = public_url.removeprefix("https://").removeprefix("http://")
    if not public_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📊 Abrir panel", web_app=WebAppInfo(url=f"https://{public_url}/app"))]])


# ──────────────── DISPATCHERS ASÍNCRONOS (comando + hub) ───────────────

async def run_address_command(chat, cmd: str, arg: str):
    """Ejecuta un comando que necesita un dato (address o pregunta).
    Usado tanto por los /comandos como por el flujo de botones del hub."""
    arg = (arg or "").strip()
    if cmd == "perfil":
        await chat.send_message("🔬 Investigando billetera… (30-60 segundos)")
        p = await asyncio.to_thread(profile_wallet, arg)
        await _send_md(chat, format_profile(p))
    elif cmd == "ficha":
        await chat.send_message("🧮 Calculando Wallet Score… (~1 min)")
        ficha = await asyncio.to_thread(_ficha_text, arg)
        await _send_md(chat, ficha or "Sin transacciones para esa dirección.")
    elif cmd == "adn":
        await chat.send_message("🧬 Componiendo el Wallet DNA… (~1 min)")
        from dna import wallet_dna_text
        text = await asyncio.to_thread(wallet_dna_text, arg)
        await _send_md(chat, text or "Sin transacciones para esa dirección.")
    elif cmd == "prediccion":
        from influence import predict_text
        text = await asyncio.to_thread(predict_text, arg)
        await _send_md(chat, text)
    elif cmd == "similar":
        from similarity import similar_text
        text = await asyncio.to_thread(similar_text, arg)
        await _send_md(chat, text)
    elif cmd == "token":
        await chat.send_message("🧬 Analizando el token…")
        from token_report import token_report
        try:
            rep = await asyncio.to_thread(token_report, arg)
        except Exception as e:
            print(f"· token_report falló: {e}")
            await chat.send_message(
                "No pude analizar ese token ahora (la fuente de datos falló). "
                "Inténtalo de nuevo en un momento.")
            return
        if rep.get("found"):
            kb = _token_keyboard(rep.get("url"), arg)
            await _send_md(chat, rep["text"], reply_markup=kb,
                           disable_web_page_preview=True)
        else:
            await chat.send_message(
                "No encontré datos de token para esa dirección. "
                "Si es una billetera usa /ficha <address>.")
    elif cmd == "entidad":
        from entity_resolution import format_entity
        text = await asyncio.to_thread(format_entity, arg)
        await _send_md(chat, text or "No detecto que esa wallet forme "
                       "parte de una entidad multi-wallet.")
    elif cmd == "ia":
        await chat.send_message("🧠 Perfilando y consultando a la IA… (~1 min)")
        text = await asyncio.to_thread(_ia_text, arg)
        await _send_md(chat, text)
    elif cmd == "evidencia":
        text = await asyncio.to_thread(_evidencia_text, arg)
        # (Ola 18-E) Por `_send_md`, no por `send_message` a pelo: es
        # donde estan el recorte al tope de Telegram, el reintento en
        # texto plano y el registro del fallo en /errores.
        await _send_md(chat, text)
    elif cmd == "descartar":
        msg = await asyncio.to_thread(discard_wallet, arg)
        await chat.send_message(msg)
    elif cmd == "rastrear":
        msg = await asyncio.to_thread(restore_wallet, arg)
        await chat.send_message(msg)
    elif cmd == "preguntar":
        await chat.send_message("🤔 Consultando la base…")
        from ai_chat import answer_question
        resp = await asyncio.to_thread(answer_question, arg)
        await chat.send_message(resp)


# ─────────────────────────── JOBS PERIÓDICOS ──────────────────────────

async def backup_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import send_db_backup
        await asyncio.to_thread(send_db_backup)
    except Exception as e:
        print(f"· backup_job falló: {e}")
        raise                  # (Ola 17-B) que el reloj de ÉXITO no se marque


async def watchdog_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from maintenance import watchdog_check, purgar_historial_bots
        await asyncio.to_thread(purgar_historial_bots)
        await asyncio.to_thread(watchdog_check)
    except Exception as e:
        print(f"· watchdog_job falló: {e}")


async def salud_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 6 h revisa la salud del sistema. Solo avisa si algo crítico
    falla (anti-ruido: no repite el mismo aviso en 12 h)."""
    try:
        from salud import revisar_y_avisar
        await asyncio.to_thread(revisar_y_avisar)
    except Exception as e:
        print(f"· salud_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "salud_job", e)
        except Exception:
            pass
        raise                  # (Ola 17-B) que el reloj de ÉXITO no se marque


async def performance_review_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Cada 24h: el rendimiento MEDIDO decide quién conserva la ⭐.
    Degrada a las billeteras cuyas señales resultaron perdedoras."""
    try:
        from performance_review import review_tracked
        _res = await asyncio.to_thread(review_tracked)
        if isinstance(_res, dict) and _res.get("error"):
            raise RuntimeError(_res["error"])
    except Exception as e:
        print(f"· performance_review_job falló: {e}")
        raise                  # (Ola 17-B) que el reloj de ÉXITO no se marque


async def learning_job(ctx: ContextTypes.DEFAULT_TYPE):
    # (Ola 17-B) Los dos aprendizajes son independientes: si uno falla,
    # el otro se intenta igual, pero al final se propaga el fallo para
    # que `_con_reloj` NO marque el reloj de éxito.
    _fallo = None
    try:
        from maintenance import weekly_learning
        await asyncio.to_thread(weekly_learning)
    except Exception as e:
        print(f"· learning_job falló: {e}")
        _fallo = e
    # Aprendizaje de qué tokens valen la pena (independiente del de señales)
    try:
        from token_learning import analyze_submitted
        await asyncio.to_thread(analyze_submitted)
    except Exception as e:
        print(f"· aprendizaje de tokens falló: {e}")
        _fallo = _fallo or e
    if _fallo is not None:
        raise _fallo


async def rescate_precios_job(ctx: ContextTypes.DEFAULT_TYPE):
    """(Ola 17-J) Cada 2 min: recupera el precio de entrada de las señales
    que se quedaron sin él porque DexScreener aún no tenía el par.

    Corre a menudo y con ventana corta a propósito: el precio solo sirve
    si se consigue en los primeros minutos (`RESCATE_MAX_S`). Lo que se
    recupera queda marcado con su retraso real en `price_lag_s`."""
    try:
        from signal_tracker import rescatar_precios
        await asyncio.to_thread(rescatar_precios)
    except Exception as e:
        print(f"· rescate_precios_job falló: {e}")


async def track_outcomes_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: mide el resultado (1h/24h) de las señales."""
    try:
        from signal_tracker import track_outcomes
        await asyncio.to_thread(track_outcomes)
    except Exception as e:
        print(f"· track_outcomes falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "track_outcomes", e)
        except Exception:
            pass


async def _precalentar_grafos():
    """Deja listas las dos cachés que el camino caliente NO puede construir.

    (Ola 17-D) Clusters y (Ola 18-C) influencia: los dos grafos se
    construían dentro del hilo de ingesta cada vez que una ⭐ compraba, y
    mientras tanto ese hilo no atendía nada. Ahora ambos se piden con
    `construir=False` desde ahí, así que alguien tiene que construirlos
    fuera. Eso se hace aquí, en un hilo aparte: cada 10 min desde
    `predictions_job` y una vez al arranque, porque hasta que estén no se
    abre ninguna predicción — y esas compras no vuelven.
    """
    try:
        from clusters import precalentar
        await asyncio.to_thread(precalentar)
    except Exception as e:
        print(f"· No pude precalentar los clusters: {e}")
    try:
        from influence import precalentar as _precal_inf
        await asyncio.to_thread(_precal_inf)
    except Exception as e:
        print(f"· No pude precalentar el grafo de influencia: {e}")


async def warmup_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Una sola vez, poco después de arrancar (Ola 18-C)."""
    await _precalentar_grafos()


async def predictions_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Evalúa predicciones vencidas y rellena rendimiento del token.
    Fuera del webhook: aquí sí puede hacer llamadas de red sin bloquear."""
    await _precalentar_grafos()
    try:
        from predictions import run_maintenance
        await asyncio.to_thread(run_maintenance)
    except Exception as e:
        print(f"· predictions_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "predictions_job", e)
        except Exception:
            pass


async def hypotheses_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Descubrimiento autónomo: la IA propone hipótesis cada 12 h."""
    try:
        from hypotheses import generate_hypotheses
        await asyncio.to_thread(generate_hypotheses)
    except Exception as e:
        print(f"· hypotheses_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "hypotheses_job", e)
        except Exception:
            pass
        raise                  # (Ola 17-B) que el reloj de ÉXITO no se marque


async def paper_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: revisa TP/SL/tiempo de las posiciones simuladas."""
    try:
        from paper_trading import update_open_trades
        await asyncio.to_thread(update_open_trades)
    except Exception as e:
        print(f"· paper_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "paper_job", e)
        except Exception:
            pass


def _resumen_diario_text() -> str:
    import time as _t
    conn = get_conn()
    ahora = int(_t.time())
    n24 = conn.execute(
        "SELECT COUNT(*) c FROM signals WHERE ts>=? AND side='compra'",
        (ahora - 86400,)).fetchone()["c"]
    med = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN chg_24h>0 THEN 1 ELSE 0 END) w "
        "FROM signals WHERE ts>=? AND side='compra' "
        "AND chg_24h IS NOT NULL", (ahora - 7 * 86400,)).fetchone()
    stars = conn.execute(
        "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"]
    conn.close()
    out = ["☀️ *Resumen diario*\n",
           f"Señales de compra (24h): {n24}",
           f"⭐ rastreadas: {stars}"]
    if med["c"]:
        wr = 100.0 * (med["w"] or 0) / med["c"]
        out.append(f"Win rate 7 días: {wr:.0f}% ({med['c']} medidas)")
    out.append("\nDetalle: /rendimiento · Simulación: /backtest")
    return "\n".join(out)


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID:
        return
    try:
        try:
            from digest import resumen_text
            txt = await asyncio.to_thread(resumen_text)
        except Exception:
            txt = await asyncio.to_thread(_resumen_diario_text)
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt,
                                   parse_mode="Markdown")
    except Exception as e:
        print(f"· resumen diario falló: {e}")


async def post_mortem_job(ctx: ContextTypes.DEFAULT_TYPE):
    """(Ola 11) Semanal: la IA revisa sus decisiones vs lo medido."""
    if not ADMIN_ID:
        return
    try:
        from post_mortem import generar
        txt = await asyncio.to_thread(generar)
        try:
            await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt,
                                       parse_mode="Markdown")
        except Exception:               # (Ola 15 - M8) sin perder el informe
            await ctx.bot.send_message(chat_id=ADMIN_ID, text=txt)
    except Exception as e:
        print(f"· post-mortem semanal falló: {e}")
        raise                  # (Ola 17-B) que el reloj de ÉXITO no se marque


async def radar_job(ctx: ContextTypes.DEFAULT_TYPE):
    """(Ola 14) Cada 15 min: tokens recién nacidos con smart money."""
    try:
        from radar import escanear
        await asyncio.to_thread(escanear)
    except Exception as e:
        print(f"· radar_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "radar", e)
        except Exception:
            pass


async def auto_cycle_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job periódico: corre el ciclo y avisa al admin."""
    resumen = await asyncio.to_thread(run_full_cycle)
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🤖 Ciclo automático:\n\n{resumen}\n\nUsa /top para ver billeteras.")
        except Exception as e:
            print(f"No se pudo avisar al admin: {e}")


async def sync_webhook_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Re-sincroniza el webhook de Helius con las billeteras ⭐ actuales, por
    si alguna se volvió rastreada entre ciclos (así ninguna se queda sin
    monitorear hasta el próximo ciclo de 6h)."""
    try:
        msg = await asyncio.to_thread(sync_helius_webhook)
        print(f"📡 Re-sync webhook: {msg}")
    except Exception as e:
        print(f"· sync_webhook_job falló: {e}")
        try:
            from errores import record
            await asyncio.to_thread(record, "sync_webhook_job", e)
        except Exception:
            pass


# ─────────────────────────── SEGURIDAD ────────────────────────────────

def solo_admin(func):
    """Decorador: ignora mensajes de cualquiera que no sea el dueño."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if ADMIN_ID and (not update.effective_user
                         or update.effective_user.id != ADMIN_ID):
            try:
                if update.message:
                    await update.message.reply_text("⛔ No autorizado.")
            except Exception:
                pass
            return
        return await func(update, ctx)
    return wrapper


# ─────────────────────────── CALLBACKS ────────────────────────────────

async def _hub_run(q, name: str):
    """Ejecuta una acción de botón del hub (h:run:<name>)."""
    chat = q.message.chat
    if name == "status":
        await q.answer()
        _txt = await asyncio.to_thread(_status_text)    # (Ola 15 - M5)
        await q.edit_message_text(_txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name in ("top10", "top20"):
        limit = 10 if name == "top10" else 20
        await q.answer()
        text, kb = await asyncio.to_thread(build_top_message, limit)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    elif name == "rendimiento":
        await q.answer()
        from rendimiento import rendimiento_text
        txt = await asyncio.to_thread(rendimiento_text)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "backtest":
        await q.answer()
        from rendimiento import backtest_text
        txt = await asyncio.to_thread(backtest_text, 0.5)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "saldos":
        await q.answer("💰 Consultando saldos…")
        txt = await asyncio.to_thread(_saldos_text)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "hermanas":
        await q.answer("🔗 Buscando vínculos… (~1 min)")
        from wallet_links import find_links
        txt = await asyncio.to_thread(find_links)
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "senales":
        await q.answer()
        _txt = await asyncio.to_thread(_senales_text)   # (Ola 15 - M5)
        await q.edit_message_text(_txt, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif name == "ciclo":
        await q.answer("⏳ Iniciando ciclo…")
        await chat.send_message("⏳ Ciclo completo iniciado…")
        resumen = await asyncio.to_thread(run_full_cycle)
        await chat.send_message(resumen)
    elif name == "descubrir":
        await q.answer("⏳ Buscando…")
        await chat.send_message("⏳ Buscando tokens ganadores…")
        saved = await asyncio.to_thread(run_discovery)
        await chat.send_message(f"✅ {saved} tokens ganadores guardados.")
    elif name == "analizar":
        await q.answer("⏳ Analizando…")
        await chat.send_message("⏳ Analizando compradores tempranos…")
        await asyncio.to_thread(run_analysis)
        await chat.send_message("✅ Análisis terminado. Usa /top.")


async def handle_hub(q, ctx: ContextTypes.DEFAULT_TYPE):
    """Router de navegación del hub (callback_data que empieza con 'h:')."""
    action = (q.data or "")[2:]
    if action == "home":
        await q.answer()
        await q.edit_message_text(hub_text(), parse_mode="Markdown",
                                  reply_markup=kb_home())
    elif action == "help":
        await q.answer()
        await q.edit_message_text(HELP_TEXT, parse_mode="Markdown",
                                  reply_markup=kb_solo_inicio())
    elif action in SECCIONES:
        texto, kb = SECCIONES[action]
        await q.answer()
        await q.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb())
    elif action == "app":
        kb = app_keyboard()
        if not kb:
            await q.answer("Falta PUBLIC_URL para el panel.", show_alert=True)
            return
        await q.answer()
        await q.message.chat.send_message(
            "Tu panel visual — top de billeteras, señales y stats en vivo:",
            reply_markup=kb)
    elif action.startswith("run:"):
        await _hub_run(q, action[4:])
    elif action.startswith("ask:"):
        cmd = action[4:]
        AWAITING[q.from_user.id] = cmd
        await q.answer()
        await q.edit_message_text(
            ASK_PROMPTS.get(cmd, "Envíame el dato:"),
            parse_mode="Markdown", reply_markup=kb_cancelar())
    else:
        await q.answer()


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los botones inline: hub, /top y confirmaciones del agente."""
    q = update.callback_query
    if ADMIN_ID and q.from_user.id != ADMIN_ID:
        await q.answer("⛔ No autorizado", show_alert=True)
        return
    data = q.data or ""

    # Navegación del hub
    if data.startswith("h:"):
        # si el usuario estaba en un "envíame la dirección", lo cancelamos
        if not data.startswith("h:ask:"):
            AWAITING.pop(q.from_user.id, None)
        await handle_hub(q, ctx)
        return

    # Paper trading: ver resumen, pedir confirmación y reiniciar.
    # Va en dos pasos a propósito — 'pap:pedir' solo pregunta, y solo
    # 'pap:reset' borra. Un dedo mal puesto no debe costar el historial.
    if data.startswith("pap:"):
        accion = data[4:]
        from paper_trading import contar, resumen_text, reset
        if accion == "ver":
            await q.answer()
            txt = await asyncio.to_thread(resumen_text)
            try:
                await q.edit_message_text(txt, parse_mode="Markdown",
                                          reply_markup=kb_paper())
            except Exception:
                # Telegram rechaza editar si el texto no cambió
                await q.message.chat.send_message(
                    txt, parse_mode="Markdown", reply_markup=kb_paper())
            return
        if accion == "pedir":
            abiertas, cerradas = await asyncio.to_thread(contar)
            if abiertas + cerradas == 0:
                await q.answer("No hay nada que reiniciar.", show_alert=True)
                return
            await q.answer()
            await q.edit_message_text(
                f"🗑 *Reiniciar paper trading*\n\n"
                f"Se borrarán *{abiertas}* posiciones abiertas y "
                f"*{cerradas}* cerradas.\n"
                f"⚠️ No se puede deshacer.",
                parse_mode="Markdown",
                reply_markup=kb_paper_confirmar(abiertas, cerradas))
            return
        if accion == "reset":
            await q.answer("⏳ Borrando…")
            try:
                abiertas, cerradas = await asyncio.to_thread(reset)
            except Exception as e:
                await q.edit_message_text(f"❌ No se pudo reiniciar: {e}")
                return
            txt = await asyncio.to_thread(resumen_text)
            await q.edit_message_text(
                f"✅ *Paper trading reiniciado*\n"
                f"Borradas {abiertas} abiertas y {cerradas} cerradas.\n\n"
                + txt, parse_mode="Markdown", reply_markup=kb_paper())
            return
        await q.answer()
        return

    # Feedback 👍/👎 sobre un token enviado (aprendizaje)
    if data.startswith("tk:up:") or data.startswith("tk:dn:"):
        good = data.startswith("tk:up:")
        mint = data.split(":", 2)[2]
        try:
            from token_learning import set_feedback
            _guardado = await asyncio.to_thread(set_feedback, mint, good)
        except Exception:
            _guardado = False
        # (Ola 17-E) Solo dar las gracias si de verdad se guardo. Antes
        # se contestaba "lo tendre en cuenta" aunque la ficha no
        # existiera y el voto se perdiera.
        if _guardado:
            await q.answer("👍 ¡Gracias! Lo tendré en cuenta."
                           if good else "👎 Anotado, aprenderé de esto.")
        else:
            await q.answer("No pude guardar tu voto: no tengo la ficha de "
                           "ese token. Vuelve a pedirlo con /token y "
                           "márcalo otra vez.")
        return

    # Botones bajo las alertas de señal
    if data.startswith("ficha:") or data.startswith("saldo1:") \
            or data.startswith("adel:"):
        addr = data.split(":", 1)[1]
        if data.startswith("adel:"):
            msg = await asyncio.to_thread(discard_wallet, addr)
            await q.answer((msg or "Hecho")[:190])
            return
        await q.answer("⏳ Consultando…")
        if data.startswith("ficha:"):
            txt = await asyncio.to_thread(_ficha_text, addr)
            txt = txt or "Sin datos para esa dirección."
        else:
            txt = await asyncio.to_thread(_saldo_uno_text, addr)
        await _send_md(q.message.chat, txt)
        return

    # Confirmación de acciones del agente IA
    if data.startswith("agc:y") or data.startswith("agc:n"):
        _partes = data.split(":")
        _tok_msg = _partes[2] if len(_partes) > 2 else ""
        _pend = PENDING_ACTIONS.get(q.from_user.id)
        if _pend is not None and _pend.get("tok") != _tok_msg:
            # (Ola 15 - A2) Boton de una propuesta VIEJA: no ejecutar la
            # nueva por accidente.
            await q.answer("Esta propuesta caducó (hay una más nueva)")
            try:
                await q.edit_message_text(
                    "⌛ Propuesta caducada: hay una más reciente abajo.")
            except Exception:
                pass
            return
        _pend = PENDING_ACTIONS.pop(q.from_user.id, None)
        accion = _pend.get("accion") if _pend else None
        _ya = ACCIONES_CONSUMIDAS.get((q.from_user.id, _tok_msg))
        if accion is None and _ya and data.startswith("agc:y"):
            # (Ola 17-B) Doble toque en el MISMO botón: la primera
            # pulsación ya se llevó la acción. No decir "expiró" ni
            # invitar a repetirla — se ejecutaría dos veces.
            await q.answer({"curso": "Ya la estoy ejecutando",
                            "cancelada": "Ya la cancelaste"}.get(
                                _ya[0], "Ya se ejecutó"))
            return
        if accion is None and data.startswith("agc:y"):
            # (Ola 16) El bot se reinició con la propuesta pendiente (vive
            # en memoria): antes decía "❌ Acción cancelada", como si el
            # dueño la hubiera cancelado él.
            await q.answer("La propuesta expiró (el bot se reinició)")
            try:
                await q.edit_message_text(
                    "⌛ Propuesta expirada (el bot se reinició). "
                    "Vuelve a pedirla y la ejecuto.")
            except Exception:
                pass
            return
        if data.startswith("agc:n") or not accion:
            _marcar_consumida(q.from_user.id, _tok_msg, "cancelada")
            await q.answer("Cancelado")
            try:
                await q.edit_message_text("❌ Acción cancelada.")
            except Exception:
                pass
            # (21/8) El agente también debe recordar los rechazos.
            if accion:
                try:
                    from ai_agent import _save_turn, describe_action
                    await asyncio.to_thread(
                        _save_turn, "(cancelé la acción propuesta)",
                        f"(acción CANCELADA: {describe_action(accion)})")
                except Exception:
                    pass
            return
        # (Ola 17-E) Marcar ANTES del `await`: entre el `pop` y esta
        # linea habia una llamada de red a Telegram, y con
        # concurrent_updates(8) el segundo toque entraba justo en ese
        # hueco y volvia a ver "propuesta expirada". La marca es lo
        # primero que se hace tras quedarse con la accion.
        _marcar_consumida(q.from_user.id, _tok_msg, "curso")
        await q.answer("Ejecutando…")
        from ai_agent import execute_action
        try:
            resultado = await asyncio.to_thread(execute_action, accion)
        finally:
            _marcar_consumida(q.from_user.id, _tok_msg, "hecha")
        try:
            await q.edit_message_text(f"✅ {resultado}")
        except Exception:
            pass
        # (21/8) Guardar el DESENLACE en la memoria del chat: sin esto,
        # el agente recordaba que propuso la acción pero no que se
        # ejecutó, y ante un "¿listo?" la volvía a proponer.
        try:
            from ai_agent import _save_turn, describe_action
            await asyncio.to_thread(
                _save_turn,
                f"(confirmé la acción: {describe_action(accion)})",
                f"(acción EJECUTADA — resultado: {resultado})")
        except Exception as e:
            print(f"· No pude guardar el desenlace en el historial: {e}")
        return

    # Botones del /top: descartar (d:) o cambiar tamaño (t:)
    if data.startswith("d:"):
        try:
            _, limit, address = data.split(":", 2)
            limit = int(limit)
        except ValueError:
            await q.answer("Dato inválido")
            return
        msg = await asyncio.to_thread(discard_wallet, address)
        await q.answer(msg[:190])
        text, kb = await asyncio.to_thread(build_top_message, limit)
        try:
            await q.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=kb)
        except Exception:
            pass  # el mensaje no cambió o expiró
    elif data.startswith("t:"):
        try:
            limit = int(data[2:])
        except ValueError:
            await q.answer()
            return
        await q.answer()
        text, kb = await asyncio.to_thread(build_top_message, limit)
        try:
            await q.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=kb)
        except Exception:
            pass


# ─────────────────────────── COMANDOS ─────────────────────────────────

@solo_admin
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AWAITING.pop(update.effective_user.id, None)
    await update.message.reply_text(hub_text(), parse_mode="Markdown",
                                    reply_markup=kb_home())


@solo_admin
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@solo_admin
async def cmd_descubrir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Buscando tokens ganadores…")
    saved = await asyncio.to_thread(run_discovery)
    await update.message.reply_text(f"✅ {saved} tokens ganadores guardados.")


@solo_admin
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando compradores tempranos…")
    await asyncio.to_thread(run_analysis)
    await update.message.reply_text("✅ Análisis terminado. Usa /top.")


@solo_admin
async def cmd_ciclo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Ciclo completo iniciado…")
    resumen = await asyncio.to_thread(run_full_cycle)
    await update.message.reply_text(resumen)


@solo_admin
async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    limit = 10
    if ctx.args:
        try:
            limit = max(5, min(30, int(ctx.args[0])))
        except ValueError:
            pass
    text, kb = await asyncio.to_thread(build_top_message, limit)
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=kb)


@solo_admin
async def cmd_descartar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /descartar <address>")
        return
    await run_address_command(update.message.chat, "descartar", ctx.args[0])


@solo_admin
async def cmd_rastrear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /rastrear <address>")
        return
    await run_address_command(update.message.chat, "rastrear", ctx.args[0])


@solo_admin
async def cmd_evidencia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /evidencia <address>")
        return
    await run_address_command(update.message.chat, "evidencia", ctx.args[0])


@solo_admin
async def cmd_perfil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /perfil <address>")
        return
    await run_address_command(update.message.chat, "perfil", ctx.args[0])


@solo_admin
async def cmd_ia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ia <address>")
        return
    await run_address_command(update.message.chat, "ia", ctx.args[0])


@solo_admin
async def cmd_ficha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /ficha <address>")
        return
    await run_address_command(update.message.chat, "ficha", ctx.args[0])


@solo_admin
async def cmd_preguntar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: /preguntar <pregunta>\nEj: /preguntar ¿qué billetera "
            "tuvo mejor resultado en sus señales?")
        return
    await run_address_command(update.message.chat, "preguntar",
                              " ".join(ctx.args))


def _alias_md(s) -> str:
    """Alias seguro para Markdown (Ola 6 - M21): un apodo con _, ` o [
    rompia el parseo y Telegram rechazaba el MENSAJE ENTERO (/elite,
    /saldos y el digest de las 13:00 fallaban en silencio)."""
    return (str(s or "").replace("*", "").replace("_", " ")
            .replace("`", "").replace("[", "(").replace("]", ")"))


@solo_admin
async def cmd_senales(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # to_thread (Ola 6 - M25): las consultas corrian EN el event loop;
    # un stall de SQLite (busy_timeout 30 s) congelaba el bot entero.
    texto = await asyncio.to_thread(_senales_text)
    await update.message.reply_text(texto, parse_mode="Markdown")


@solo_admin
async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Abre el panel visual (Mini App de Telegram)."""
    kb = app_keyboard()
    if not kb:
        await update.message.reply_text("Falta PUBLIC_URL para el panel.")
        return
    await update.message.reply_text(
        "Tu panel visual — top de billeteras, señales y stats en vivo:",
        reply_markup=kb)


@solo_admin
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = await asyncio.to_thread(_status_text)
    await update.message.reply_text(texto, parse_mode="Markdown")


# (Ola 18-B) Modo COPIA PURA del paper.
#
# La simulación arrastraba once reglas propias con valor por defecto que
# el dueño nunca configuró: take-profit, stop-loss, reloj, enfriamiento,
# tope de posiciones, hold extra con trailing, y un A/B que dejaba la
# salida en manos de la IA en la mitad de las posiciones. Eso NO es
# copiar: es otra estrategia montada encima. Y su objetivo declarado es
# medir la suya — comprar cuando la ⭐ compra, vender cuando vende.
#
# Estos ajustes se escriben en `settings` y NO había forma de cambiarlos
# desde el bot. Escribirlos desde fuera significaría abrir la base que el
# proceso tiene viva, y el histórico es lo único irreversible del
# sistema: se hace desde dentro, con la conexión de siempre.
#
# `on` guarda los valores anteriores y aplica los nuevos; `off` restaura
# EXACTAMENTE lo que había. Reversible de verdad, no "vuelve al defecto".
_COPIA_PURA = {
    "paper_tp_pct": "999999",         # no cortar las ganadoras
    "paper_sl_pct": "999999",         # no vender en el suelo
    "paper_timeout_h": "999999",      # no cerrar por reloj
    # `paper_reentrada_h` YA NO esta aqui (26/8/2026): lo gobierna el mando
    # /reentrada. Cuando este preset lo ponia a 0, un mismo token llego a
    # abrirse 24 veces en un dia (varias ⭐ turnandose), y /copiapura on
    # pisaba en silencio lo que el dueño acababa de fijar con /reentrada.
    # Al quitarlo de este dict, el `off` tampoco restaura el valor viejo
    # de fotos guardadas antes del cambio (el bucle de restauracion filtra
    # por las claves de ESTE dict a proposito).
    "paper_max_abiertas": "50",       # no descartar señales en los picos
    "paper_parcial_min_pct": "0",     # copiar también las ventas pequeñas
    "paper_total_pct": "100",         # cerrar solo cuando ella cierra
    "paper_hold_extra": "0",          # no quedarse dentro tras su venta
    "ia_local_activa": "0",           # que la IA no decida las salidas
}


@solo_admin
async def cmd_copia_pura(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Enciende o apaga el modo copia pura del paper trading.

    /copiapura on   → el paper copia y nada más
    /copiapura off  → restaura los valores que había antes
    /copiapura      → dice en qué modo está
    """
    import json as _json
    from db import get_setting, set_setting

    def _trabajo(accion: str) -> str:
        conn = get_conn()
        try:
            previo_raw = get_setting(conn, "copia_pura_previo", None)
            activo = bool(previo_raw)
            if not accion:
                if activo:
                    return ("🧬 *Copia pura: ENCENDIDA*\nEl paper compra "
                            "cuando la ⭐ compra y vende cuando ella vende. "
                            "Sin TP, sin SL, sin reloj, sin hold extra.\n"
                            "Apagar: `/copiapura off`")
                return ("🧬 *Copia pura: apagada*\nEl paper aplica sus "
                        "reglas propias (TP +100%, SL −50%, reloj de 48 h, "
                        "hold extra, y la IA decide la mitad de las "
                        "salidas).\nEncender: `/copiapura on`")
            if accion == "on":
                if activo:
                    return "🧬 Ya estaba encendida. `/copiapura off` la quita."
                previo = {k: get_setting(conn, k, None)
                          for k in _COPIA_PURA}
                set_setting(conn, "copia_pura_previo",
                            _json.dumps(previo, ensure_ascii=False))
                for k, v in _COPIA_PURA.items():
                    set_setting(conn, k, v)
                conn.commit()
                return ("🧬 *Copia pura ENCENDIDA.*\n\nEl paper ya solo hace "
                        "una cosa: entra cuando la ⭐ entra y sale cuando "
                        "ella sale.\n\n"
                        "• take-profit, stop-loss y reloj: apagados\n"
                        "• hold extra y trailing: apagados\n"
                        "• enfriamiento por token: NO se toca "
                        "(se maneja con /reentrada)\n"
                        "• tope de posiciones: 50\n"
                        "• la IA ya no decide salidas\n\n"
                        "_Los rugs siguen cerrándose: esa vía no depende "
                        "del reloj._\n\n"
                        "⚠️ Lo cerrado ANTES se midió con las reglas "
                        "viejas. Para comparar, separa los dos periodos.")
            if accion == "off":
                if not activo:
                    return "🧬 Ya estaba apagada."
                try:
                    previo = _json.loads(previo_raw)
                except (TypeError, ValueError):
                    return ("⚠️ No pude leer los valores anteriores; no "
                            "toco nada para no dejarlo a medias.")
                for k, v in previo.items():
                    if k in _COPIA_PURA:
                        set_setting(conn, k, v if v is not None else "")
                # Residuo del preset VIEJO (auditoria 18-K, ronda 2): las
                # versiones anteriores ponian paper_reentrada_h a "0" al
                # encender. Como la clave ya no esta en _COPIA_PURA, el
                # bucle de arriba no la restaura, y ese 0 huerfano dejaria
                # al mismo token reabriendose en cadena para siempre. Solo
                # se toca si el valor actual es LITERALMENTE la cadena
                # "0" que escribio el preset viejo: /reentrada guarda el
                # numero como float ("0.0", "6.0"), asi que un
                # `/reentrada 0` deliberado NO coincide y se respeta
                # (ronda 3 de la auditoria).
                _linea_reent = ""
                if "paper_reentrada_h" in previo:
                    _act = get_setting(conn, "paper_reentrada_h", None)
                    _es_cero = (str(_act).strip() == "0")
                    if _es_cero:
                        _v = previo["paper_reentrada_h"]
                        set_setting(conn, "paper_reentrada_h",
                                    _v if _v is not None else "")
                        _linea_reent = (
                            "\nEl enfriamiento por token también vuelve a "
                            f"*{_v or '24'} h* (lo había dejado a 0 una "
                            "versión vieja de este modo; ajústalo con "
                            "/reentrada).")
                set_setting(conn, "copia_pura_previo", "")
                conn.commit()
                return ("🧬 *Copia pura apagada.* Restaurados los valores "
                        "que había antes de encenderla." + _linea_reent)
            return "Uso: `/copiapura on` · `/copiapura off` · `/copiapura`"
        finally:
            conn.close()

    accion = (ctx.args[0].strip().lower() if ctx.args else "")
    if accion and accion not in ("on", "off"):
        await update.message.reply_text(
            "Uso: `/copiapura on` · `/copiapura off` · `/copiapura`",
            parse_mode="Markdown")
        return
    txt = await asyncio.to_thread(_trabajo, accion)
    await _send_md(update.message.chat, txt)


def _nota_conteos(conn) -> dict:
    """Cuántas ⭐ hay por nota, contadas como las cuenta quien QUITA la ⭐.

    (Ola 18-H) `flojas` incluía las de nota NULL, pero
    `ai_analyst.depurar_estrellas` exige `grade IS NOT NULL` para quitar
    la estrella: a las que aún no tiene puntuadas el embudo NO se las
    toca. Con 3 sin nota, el mensaje decía "hasta 6 la pierden" cuando
    eran 3 — y el desvío es mayor justo después de un despliegue, que es
    cuando más ⭐ están sin puntuar (el embudo repuntúa 20-25 cada 6 h).
    Es el número que el dueño mira para decidir, así que tiene que ser el
    de verdad.
    """
    fila = conn.execute(
        """SELECT
             SUM(CASE WHEN grade IN ('Elite','Seguimiento')
                      THEN 1 ELSE 0 END) buenas,
             SUM(CASE WHEN grade IS NOT NULL
                       AND grade NOT IN ('Elite','Seguimiento')
                      THEN 1 ELSE 0 END) flojas,
             SUM(CASE WHEN grade IS NULL THEN 1 ELSE 0 END) sin_nota,
             COUNT(*) total
           FROM wallets WHERE is_tracked = 1""").fetchone()
    return {"buenas": int(fila["buenas"] or 0),
            "flojas": int(fila["flojas"] or 0),
            "sin_nota": int(fila["sin_nota"] or 0),
            "total": int(fila["total"] or 0)}


@solo_admin
async def cmd_nota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """¿La nota del embudo decide quién lleva ⭐?  /nota [on|off]

    Sin argumento NO cambia nada: enseña cuántas ⭐ hay por nota y cuántas
    perderían la estrella al encenderlo, con las cifras de la base de
    ahora mismo. Encenderlo es una decisión con consecuencias (la lista se
    hace mucho más corta), así que conviene mirar el número antes.
    """
    from db import get_setting, set_setting

    def _trabajo(accion: str) -> str:
        conn = get_conn()
        try:
            activo = (get_setting(conn, "grado_vinculante", "0")
                      or "0").strip() == "1"
            cuentas = _nota_conteos(conn)
            buenas = cuentas["buenas"]
            flojas = cuentas["flojas"]
            sinnota = cuentas["sin_nota"]
            if accion == "on":
                set_setting(conn, "grado_vinculante", "1")
                return (f"⭐ *La nota del embudo MANDA*\n\n"
                        f"Solo llevan ⭐ las billeteras con nota *Elite* o "
                        f"*Seguimiento*.\n"
                        f"Con las notas de ahora la pierden *{flojas}*; se "
                        f"quedan *{buenas}* de nota buena y *{sinnota}* que "
                        f"aún no tienen nota (a las sin nota no se les "
                        f"quita: hasta que el embudo las puntúe, siguen).\n\n"
                        f"_Las que la pierden NO se borran: siguen en la "
                        f"base y pueden recuperarla si su nota mejora._\n"
                        f"⚠️ _Apagar tarda: al perder la ⭐ pasan a la cola "
                        f"de re-evaluación de rechazadas, que es de 14 días. "
                        f"Encender es inmediato; volver atrás, no._\n"
                        f"Volver atrás: `/nota off`")
            if accion == "off":
                set_setting(conn, "grado_vinculante", "0")
                return ("⭐ *La nota del embudo es solo informativa*\n\n"
                        "Se vuelve al criterio de siempre: la ⭐ la decide "
                        "la IA, y solo se quita a las *Descartada*.\n\n"
                        "⚠️ _Las que ya la perdieron NO vuelven al momento: "
                        "entran en la cola de re-evaluación de rechazadas "
                        "(hasta 14 días) y recuperan la ⭐ cuando les toque._"
                        "\n\nEncender: `/nota on`")
            estado = ("🟢 *ENCENDIDA*" if activo
                      else "⚪ *apagada* (solo informativa)")
            return (f"⭐ *Nota del embudo:* {estado}\n\n"
                    f"Tus ⭐ ahora: *{cuentas['total']}*\n"
                    f"  · con nota buena (Elite/Seguimiento): *{buenas}*\n"
                    f"  · con nota floja: *{flojas}*\n"
                    f"  · sin nota todavía: *{sinnota}*\n\n"
                    + (f"Con `/nota on` se quedarían *{buenas}* de nota "
                       f"buena y las *{sinnota}* que aún no tienen nota "
                       f"(pierden la ⭐ las *{flojas}* de nota floja).\n\n"
                       "_Dos avisos antes de encenderlo:_\n"
                       "_1) Las que hoy no son ⭐ y tienen nota buena NO "
                       "la recuperan — están fuera por otros motivos "
                       "(insider, pérdidas medidas, misma familia de "
                       "fondos), y eso la nota no lo ve._\n"
                       "_2) Estas cuentas salen de la nota que cada "
                       "billetera tiene GUARDADA, y esa nota se recalcula "
                       "poco a poco (unas 20-25 cada 6 h). Si acabas de "
                       "actualizar el bot, el reparto real cambiará según "
                       "se vayan re-evaluando._"
                       if not activo else
                       "Con `/nota off` vuelve a mandar solo la IA."))
        finally:
            conn.close()

    accion = (ctx.args[0].lower().strip() if ctx.args else "")
    if accion not in ("", "on", "off"):
        await update.message.reply_text("Uso: /nota [on|off]")
        return
    txt = await asyncio.to_thread(_trabajo, accion)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_top_alertas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cuántas billeteras del ranking pueden alertar. /topalertas [n]

    0 = sin límite (vuelve al comportamiento de antes: alerta cualquier ⭐).
    """
    from db import set_setting, get_setting, TOP_ALERTAS_DEFAULT
    conn = get_conn()
    try:
        args = ctx.args or []
        if args:
            try:
                n = int(float(args[0]))
                if n < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "Uso: /topalertas <n>   (0 = sin límite)")
                return
            set_setting(conn, "top_alertas", n)
            txt = (f"📡 Alertan las *top {n}* billeteras."
                   if n else "📡 Sin límite: alerta cualquier billetera ⭐.")
            await update.message.reply_text(txt, parse_mode="Markdown")
            return
        actual = get_setting(conn, "top_alertas", str(TOP_ALERTAS_DEFAULT))
        # (Ola 8) 0 significa "sin limite" para todo el sistema: decir
        # "top 0" seria mentira.
        _desc = (f"las *top {actual}* billeteras"
                 if int(float(actual or 0)) else
                 "*todas* las billeteras ⭐ (sin límite)")
        await update.message.reply_text(
            f"📡 Ahora mismo alertan {_desc} "
            f"(señales y tarjetas).\nCambiar: `/topalertas 20`  ·  "
            f"`/topalertas 0` quita el límite.",
            parse_mode="Markdown")
    finally:
        conn.close()


@solo_admin
async def cmd_reentrada(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Enfriamiento por token del paper trading. /reentrada [horas]

    0 = sin enfriamiento (un token puede reabrirse al instante).

    Hacía falta un mando propio (26/8/2026): el valor solo se podía
    cambiar desde `/copiapura`, que lo deja en 0, y con 0 el mismo token
    se reabría en cadena — un caso real llegó a 24 posiciones del mismo
    token en un día porque varias ⭐ lo compraban y vendían por turnos.
    Escribirlo desde fuera significaría abrir la base que el proceso
    tiene viva; se hace desde dentro, con la conexión de siempre.
    """
    from db import set_setting, get_setting
    conn = get_conn()
    try:
        args = ctx.args or []
        if args:
            try:
                h = float(args[0])
                # NaN e inf pasarían un `h < 0` a secas.
                if not (0 <= h <= 720):
                    raise ValueError
            except (TypeError, ValueError):
                await update.message.reply_text(
                    "Uso: /reentrada <horas> entre 0 y 720  "
                    "(0 = sin enfriamiento; ej: /reentrada 6)")
                return
            set_setting(conn, "paper_reentrada_h", h)
            txt = (f"⏳ Un token ya jugado no se vuelve a abrir hasta "
                   f"*{h:g} h* después de cerrarse."
                   if h else
                   "⏳ Sin enfriamiento: un token puede reabrirse al "
                   "instante.")
            await update.message.reply_text(txt, parse_mode="Markdown")
            return
        actual = _f_setting(get_setting(conn, "paper_reentrada_h", "24"), 24.0)
        _desc = (f"*{actual:g} h*" if actual else "*sin enfriamiento*")
        await update.message.reply_text(
            f"⏳ Enfriamiento por token: {_desc}.\n"
            f"Cambiar: `/reentrada 6`  ·  `/reentrada 0`",
            parse_mode="Markdown")
    finally:
        conn.close()


def _f_setting(valor, por_defecto: float) -> float:
    """Lee un ajuste numérico sin romperse con None, '' o basura."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return por_defecto
    # NaN sobrevive a cualquier comparación; se descarta explícitamente.
    if v != v:
        return por_defecto
    return v


@solo_admin
async def cmd_paper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Paper trading: /paper (resumen) · /paper on|off · /paper max <SOL>
    · /paper reset (con confirmación)."""
    from db import set_setting
    from paper_trading import resumen_text
    args = [a.lower() for a in (ctx.args or [])]
    if args:
        if args[0] == "reset":
            # No borra aquí: enseña lo que se perdería y pide confirmación.
            from paper_trading import contar
            abiertas, cerradas = await asyncio.to_thread(contar)
            if abiertas + cerradas == 0:
                await update.message.reply_text(
                    "🧪 No hay nada que reiniciar: el historial ya está vacío.")
                return
            await update.message.reply_text(
                f"🗑 *Reiniciar paper trading*\n\n"
                f"Se borrarán *{abiertas}* posiciones abiertas y "
                f"*{cerradas}* cerradas.\n"
                f"⚠️ No se puede deshacer.",
                parse_mode="Markdown",
                reply_markup=kb_paper_confirmar(abiertas, cerradas))
            return
        conn = get_conn()
        if args[0] in ("on", "off"):
            set_setting(conn, "paper_enabled", "1" if args[0] == "on" else "0")
            estado = "activado 🟢" if args[0] == "on" else "apagado 🔴"
            conn.close()
            await update.message.reply_text(f"🧪 Paper trading {estado}.")
            return
        if args[0] == "max" and len(args) > 1:
            try:
                v = float(args[1])
                # (Ola 15 - B1) NaN e inf pasaban: `nan <= 0` es False.
                # Un stake NaN envenena pnl_usd y el win rate del paper.
                if not (0.05 <= v <= 10):
                    raise ValueError
            except ValueError:
                conn.close()
                await update.message.reply_text(
                    "Uso: /paper max <SOL> entre 0.05 y 10  "
                    "(ej: /paper max 1.5)")
                return
            set_setting(conn, "paper_max_sol", v)
            conn.close()
            await update.message.reply_text(
                f"🧪 Tope por señal: *{v:g} SOL*", parse_mode="Markdown")
            return
        conn.close()
        await update.message.reply_text(
            "Uso: /paper · /paper on · /paper off · /paper max <SOL> "
            "· /paper reset")
        return
    txt = await asyncio.to_thread(resumen_text)
    # (Ola 18-H) Por `_send_md`, no por `reply_text` a pelo: asi el
    # mensaje se recorta si pasa de 4.096 (con 15 posiciones listadas
    # cabe) y, si el Markdown se rompe igualmente, se reintenta en texto
    # plano en vez de quedarse MUDO.
    await _send_md(update.message.chat, txt, reply_markup=kb_paper())


# SIN @solo_admin (Ola 6, auditoria 19/8 - C5): esto es un helper de
# fondo, no un handler. El decorador lo envolvia como wrapper(update,
# ctx) de 2 parametros; la llamada le pasa 4 → TypeError en CADA mint
# pegado que generaba ficha, y la extraccion de compradores (la que
# alimenta la red de billeteras) no corrio NUNCA — disfrazada del
# "⚠️ Algo falló" generico. La seguridad ya la puso on_chat, que si
# es handler y si esta decorado.
async def _extract_buyers_bg(chat, mint: str, symbol, chg24):
    """En segundo plano: extrae los compradores del token enviado y los mete
    a la red (mismo análisis que un token ganador). No bloquea la ficha."""
    try:
        from token_extract import extract_buyers
        status, n = await asyncio.to_thread(
            extract_buyers, mint, symbol, chg24)
        nombre = f"${symbol}" if symbol else mint[:6]
        if status == "ok":
            msg = (f"🧠 Analicé los compradores de {nombre}: "
                   f"{n} billeteras registradas en tu red. "
                   f"Entrarán al grading en el próximo ciclo.")
            # ¿El dev vendió? (solo cuando la extracción corrió → hereda límite)
            try:
                from dev_check import dev_line
                dl = await asyncio.to_thread(dev_line, mint)
                if dl:
                    msg += "\n" + dl
            except Exception as e:
                print(f"· dev_line en bg falló: {e}")
            await chat.send_message(msg)
        elif status == "rate":
            await chat.send_message(
                "⏳ Alcancé el límite de análisis de tokens por hora "
                "(para cuidar la cuota). Intenta de nuevo más tarde.")
        elif status == "descarga":
            # (Ola 18-D) Antes esto salía como "0 billeteras registradas",
            # que se lee como "este token no tiene compradores buenos".
            await chat.send_message(
                f"⚠️ No pude descargar el historial completo de {nombre} "
                f"(Helius no respondió). No registré nada para no meter "
                f"datos a medias. Vuelve a pegar el mint cuando quieras: "
                f"no se ha guardado en caché.")
        # "cache" y "error" no molestan al usuario
    except Exception as e:
        print(f"· _extract_buyers_bg falló: {e}")


@solo_admin
async def on_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chat libre. Si el hub estaba esperando un dato, lo consume aquí;
    si no, cualquier texto sin /comando activa al agente IA."""
    texto = (update.message.text or "").strip()
    if not texto:
        return

    # ¿El usuario había pulsado un botón que pedía dirección/pregunta?
    cmd = AWAITING.pop(update.effective_user.id, None)
    if cmd:
        await run_address_command(update.message.chat, cmd, texto)
        return

    # ¿Es un contrato/mint pegado directo? → ficha de token
    if _MINT_RE.match(texto):
        await update.message.chat.send_action("typing")
        from token_report import token_report
        try:
            rep = await asyncio.to_thread(token_report, texto)
        except Exception as e:
            print(f"· token_report falló: {e}")
            await update.message.reply_text(
                "No pude analizar ese token ahora (la fuente de datos falló). "
                "Inténtalo de nuevo en un momento.")
            return
        if rep.get("found"):
            kb = _token_keyboard(rep.get("url"), texto)
            await _send_md(update.message.chat, rep["text"],
                           reply_markup=kb, disable_web_page_preview=True)
            # En segundo plano: extraer compradores → alimenta la red.
            # Se guarda la referencia: sin ella el GC puede matar la tarea.
            data = rep.get("data") or {}
            tarea = asyncio.create_task(_extract_buyers_bg(
                update.message.chat, texto,
                data.get("symbol"), data.get("price_change_h24")))
            _BG_TASKS.add(tarea)
            tarea.add_done_callback(_BG_TASKS.discard)
            return
        # no es un token tradeable (¿billetera?) → sigue el flujo normal

    await update.message.chat.send_action("typing")
    from ai_agent import chat, describe_action
    respuesta, accion = await asyncio.to_thread(chat, texto)
    if accion:
        # (Ola 15 - A2) Cada propuesta lleva su propio token: el boton
        # Confirmar solo ejecuta LA propuesta de SU mensaje. Antes, dos
        # propuestas seguidas compartian el slot y confirmar el mensaje
        # viejo ejecutaba la accion nueva.
        import secrets as _sec
        _tok = _sec.token_hex(4)
        PENDING_ACTIONS[update.effective_user.id] = {"tok": _tok,
                                                     "accion": accion}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirmar",
                                 callback_data=f"agc:y:{_tok}"),
            InlineKeyboardButton("❌ Cancelar",
                                 callback_data=f"agc:n:{_tok}")]])
        msg = (respuesta + "\n\n" if respuesta else "") + \
            f"¿Ejecuto esta acción?\n{describe_action(accion)}"
        try:
            await update.message.reply_text(msg, parse_mode="Markdown",
                                            reply_markup=kb)
        except Exception:      # (Ola 15 - M8) un "_" impar del modelo no
            await update.message.reply_text(msg, reply_markup=kb)
    else:
        await update.message.reply_text(respuesta)


# ─────────────────────────── ARRANQUE ─────────────────────────────────

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejador global: registra cualquier excepción no capturada de un
    comando. Antes se perdía en silencio y el usuario solo veía que el bot
    no respondía."""
    err = getattr(ctx, "error", None)
    print(f"· Error no capturado: {err}")
    try:
        from errores import record
        await asyncio.to_thread(record, "telegram", err, "handler")
    except Exception:
        pass
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Algo falló al procesar eso. Ya quedó registrado; "
                "míralo con /errores.")
    except Exception:
        pass


async def _post_init(app: Application):
    """Registra el menú de comandos que se ve al pulsar '/' en Telegram."""
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Abrir el menú principal"),
            BotCommand("resumen", "Resumen del día (todo en una vista)"),
            BotCommand("menu", "Abrir el menú principal"),
            BotCommand("top", "Top de billeteras"),
            BotCommand("senales", "Últimas señales"),
            BotCommand("status", "Estado del sistema"),
            BotCommand("app", "Panel visual (Mini App)"),
            BotCommand("ciclo", "Correr el pipeline ahora"),
            BotCommand("preguntar", "Preguntar a la IA sobre tu base"),
            BotCommand("rendimiento", "Win rate de las señales"),
            BotCommand("estrellasperf", "Rendimiento medido de cada ⭐"),
            BotCommand("salud", "¿Está todo funcionando bien?"),
            BotCommand("datos", "Conocimiento propio acumulado"),
            BotCommand("reevaluar", "Volver a graduar las billeteras"),
            BotCommand("exportar", "Descargar todo en JSON (para IA local)"),
            BotCommand("errores", "Errores registrados (24 h)"),
            BotCommand("backtest", "Simular copiar las señales"),
            BotCommand("paper", "Paper trading simulado"),
            BotCommand("topalertas", "Cuántas billeteras pueden alertar"),
            BotCommand("reentrada", "Horas antes de repetir un token"),
            BotCommand("saldos", "Saldo SOL de las vigiladas"),
            BotCommand("hermanas", "Billeteras del mismo dueño"),
            BotCommand("ficha", "Ficha completa de una billetera"),
            BotCommand("adn", "Wallet DNA completo de una billetera"),
            BotCommand("clusters", "Redes de co-compra detectadas"),
            BotCommand("prediccion", "Quién comprará tras una billetera"),
            BotCommand("lideres", "Líderes ocultos de la red"),
            BotCommand("predicciones", "Señales predictivas y su precisión"),
            BotCommand("metricas", "Panel de rendimiento del motor"),
            BotCommand("backup", "Descargar copia de la base de datos"),
            BotCommand("elite", "Clasificación Elite/Seguimiento/Observación"),
            BotCommand("alpha", "Quién descubre gemas antes que el mercado"),
            BotCommand("atencion", "Score de Atención (quién aporta info nueva)"),
            BotCommand("similar", "Billeteras parecidas a una dada"),
            BotCommand("estrellas", "Estrellas emergentes (clones de Elite)"),
            BotCommand("token", "Token DNA de un mint"),
            BotCommand("hipotesis", "Hipótesis autónomas del sistema"),
            BotCommand("entidad", "Wallets del mismo operador (evidencia)"),
        ])
    except Exception as e:
        print(f"· set_my_commands falló: {e}")


def _marcar_reevaluacion(solo_sin_estrella: bool = True) -> int:
    """Marca billeteras para que el próximo ciclo las vuelva a graduar.

    Se limpia `pnl_updated`, que es lo que el selector usa para saber si un
    veredicto caducó. Útil tras cambiar los criterios de grading: sin esto
    habría que esperar los días de caducidad para ver el efecto.
    """
    conn = get_conn()
    try:
        cond = ("WHERE COALESCE(is_bot,0)=0 AND ai_class IS NOT NULL"
                + (" AND COALESCE(is_tracked,0)=0" if solo_sin_estrella else ""))
        n = conn.execute(f"SELECT COUNT(*) c FROM wallets {cond}"
                         ).fetchone()["c"]
        conn.execute(f"UPDATE wallets SET pnl_updated=NULL {cond}")
        conn.commit()
        return n
    finally:
        conn.close()


@solo_admin
async def cmd_reevaluar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Vuelve a graduar las billeteras ya evaluadas (tras cambiar criterios)."""
    todas = bool(ctx.args and ctx.args[0].lower() in ("todas", "all"))
    n = await asyncio.to_thread(_marcar_reevaluacion, not todas)
    await update.message.reply_text(
        f"🔄 {n} billeteras marcadas para volver a graduarse.\n"
        f"Se irán procesando en los próximos ciclos "
        f"({config.MAX_EVAL_PER_CYCLE} por ciclo, cada "
        f"{AUTO_CYCLE_HOURS:g} h).\n\n"
        "Usa /ciclo si quieres empezar ya.")


@solo_admin
async def cmd_datos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qué conocimiento propio llevamos acumulado (sin depender de Helius)."""
    from exportar import resumen
    from trades_store import resumen_text
    txt = await asyncio.to_thread(resumen)
    txt2 = await asyncio.to_thread(resumen_text)
    await _send_md(update.message.chat, txt + "\n\n" + txt2)


@solo_admin
async def cmd_exportar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Exporta TODO el conocimiento a JSON y lo envía como archivo.
    Pensado para analizarlo fuera del bot (p. ej. con una IA local)."""
    await update.message.reply_text("📦 Preparando el export…")
    from exportar import exportar
    rutas = await asyncio.to_thread(exportar)
    if isinstance(rutas, str):        # compatibilidad por si vuelve una sola
        rutas = [rutas]
    if not rutas:
        await update.message.reply_text("No se pudo generar el export.")
        return

    import os as _os
    total = len(rutas)
    if total > 1:
        await update.message.reply_text(
            f"El export ocupa demasiado para un solo archivo, van *{total} "
            "partes*. Descárgalas todas: cada una es un JSON comprimido "
            "válido por sí mismo.", parse_mode="Markdown")

    enviadas = 0
    for i, ruta in enumerate(rutas, start=1):
        try:
            mb = _os.path.getsize(ruta) / 1e6
            cap = (f"📦 Conocimiento acumulado ({mb:.1f} MB)"
                   if total == 1 else f"📦 Parte {i}/{total} ({mb:.1f} MB)")
            with open(ruta, "rb") as fh:
                await update.message.reply_document(
                    document=fh, filename=_os.path.basename(ruta),
                    caption=cap)
            enviadas += 1
        except Exception as e:
            await update.message.reply_text(
                f"No se pudo enviar la parte {i}/{total}: {e}")
        finally:
            try:
                _os.remove(ruta)      # no dejar GB muertos en el disco
            except OSError:
                pass
    if enviadas:
        await update.message.reply_text(
            f"✅ Export completo: {enviadas}/{total} archivo(s).")


@solo_admin
async def cmd_salud(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Autodiagnóstico. '/salud ia' añade la lectura interpretada."""
    con_ia = bool(ctx.args and ctx.args[0].lower() in ("ia", "ai"))
    await update.message.chat.send_action("typing")
    from salud import salud_text
    txt = await asyncio.to_thread(salud_text, None, con_ia)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_errores(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Errores registrados en las últimas 24 h (o las que se indiquen)."""
    horas = 24
    if ctx.args:
        try:
            horas = max(1, min(168, int(ctx.args[0])))
        except ValueError:
            pass
    from errores import errores_text
    txt = await asyncio.to_thread(errores_text, horas)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_wallets_perf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rendimiento MEDIDO de cada ⭐ (no degrada nada, solo informa)."""
    from performance_review import review_text
    txt = await asyncio.to_thread(review_text)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_rendimiento(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from rendimiento import rendimiento_text
    txt = await asyncio.to_thread(rendimiento_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    monto = 0.5
    if ctx.args:
        try:
            monto = max(0.05, min(50.0, float(ctx.args[0])))
        except ValueError:
            pass
    from rendimiento import backtest_text
    txt = await asyncio.to_thread(backtest_text, monto)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_adn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /adn <address>")
        return
    await run_address_command(update.message.chat, "adn", ctx.args[0])


@solo_admin
async def cmd_clusters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕸 Buscando clusters de co-compra…")
    from clusters import clusters_text
    txt = await asyncio.to_thread(clusters_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_prediccion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /prediccion <address>")
        return
    await run_address_command(update.message.chat, "prediccion", ctx.args[0])


@solo_admin
async def cmd_lideres(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕵️ Buscando líderes ocultos…")
    from influence import hidden_leaders_text
    txt = await asyncio.to_thread(hidden_leaders_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_predicciones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from predictions import predictions_text
    txt = await asyncio.to_thread(predictions_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_metricas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from predictions import metrics_text
    txt = await asyncio.to_thread(metrics_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_elite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = await asyncio.to_thread(_elite_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_alpha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 Midiendo quién descubre antes…")
    from alpha import alpha_text
    txt = await asyncio.to_thread(alpha_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_atencion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Calculando Score de Atención…")
    from attention import attention_text
    txt = await asyncio.to_thread(attention_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Componiendo el resumen…")
    from digest import resumen_text
    txt = await asyncio.to_thread(resumen_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_similar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /similar <address>")
        return
    await run_address_command(update.message.chat, "similar", ctx.args[0])


@solo_admin
async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /token <mint>")
        return
    await run_address_command(update.message.chat, "token", ctx.args[0])


@solo_admin
async def cmd_estrellas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌱 Buscando estrellas emergentes…")
    from similarity import rising_stars_text
    txt = await asyncio.to_thread(rising_stars_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_hipotesis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Generando hipótesis del sistema…")
    from hypotheses import hypotheses_text
    txt = await asyncio.to_thread(hypotheses_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


@solo_admin
async def cmd_entidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await run_address_command(update.message.chat, "entidad", ctx.args[0])
        return
    from entity_resolution import entities_text
    txt = await asyncio.to_thread(entities_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _elite_text() -> str:
    conn = get_conn()
    # (Ola 8, 21/8) El corte a 40 era silencioso: con 280 graduadas en la
    # base, la cola desaparecia y la lista parecia completa. Ahora se dice.
    graduadas = conn.execute(
        """SELECT COUNT(*) c FROM wallets
           WHERE grade IN ('Elite','Seguimiento','Observación')
             AND is_bot=0""").fetchone()["c"]
    rows = conn.execute(
        """SELECT address, alias, grade, consistency, pnl_total, wallet_score
           FROM wallets
           WHERE grade IN ('Elite','Seguimiento','Observación') AND is_bot=0
           ORDER BY CASE grade WHEN 'Elite' THEN 0 WHEN 'Seguimiento' THEN 1
                    ELSE 2 END,
                    COALESCE(wallet_score,0) DESC,
                    COALESCE(pnl_total,-1e9) DESC
           LIMIT 40""").fetchall()
    conn.close()
    if not rows:
        return ("Aún no hay billeteras clasificadas. Se irán graduando cuando "
                "la IA re-evalúe cada billetera (cada ~3 días o con /analizar).")
    emo = {"Elite": "⭐", "Seguimiento": "🟢", "Observación": "🟡"}
    titulo = ("🏆 *Clasificación de billeteras*"
              + (f"  _(top {len(rows)} de {graduadas})_"
                 if graduadas > len(rows) else "") + "\n")
    out, actual = [titulo], None
    for r in rows:
        if r["grade"] != actual:
            actual = r["grade"]
            out.append(f"\n{emo.get(actual,'')} *{actual}*")
        nombre = _alias_md(r["alias"] or r["address"][:8])
        cons = f" · C{round(r['consistency'])}" if r["consistency"] is not None else ""
        pnl = f" · {r['pnl_total']:+.0f} SOL" if r["pnl_total"] is not None else ""
        sc = f" · score {round(r['wallet_score'])}" if r["wallet_score"] is not None else ""
        out.append(f"• {nombre}{sc}{pnl}{cons}")
    out.append("\n_C = Consistency Score. Elite = rentable, consistente y "
               "líder. PnL y score son de la última evaluación de cada "
               "billetera (se refrescan cada 3-14 días). "
               "Usa /adn <address> para el detalle._")
    return "\n".join(out)


@solo_admin
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💾 Preparando copia de la base de datos…")
    path = None
    try:
        from backup import make_backup
        path, fname, caption = await asyncio.to_thread(make_backup)

        # (Ola 17-C, auditoría 4) El manual NO copiaba a `backups/`: con
        # la base en 262 MB el envío por Telegram (límite 50 MB) muere y
        # el usuario se queda sin nada, igual que antes del arreglo del
        # automático. Primero se guarda en disco; el envío es lo opcional.
        def _guardar_en_disco():
            from maintenance import guardar_copia_en_disco
            return guardar_copia_en_disco(path, fname)

        destino = None
        try:
            destino = await asyncio.to_thread(_guardar_en_disco)
        except Exception as e:
            print(f"· /backup: no pude guardar en disco: {e}")
        if destino:
            await update.message.reply_text(
                f"✅ Copia guardada en el equipo:\n`{destino}`",
                parse_mode="Markdown")
        _mb = os.path.getsize(path) / 1e6
        if _mb > 49:
            await update.message.reply_text(
                f"({_mb:.0f} MB — demasiado grande para enviarla por "
                f"Telegram, que admite 50 MB. Está en el equipo.)")
        else:
            with open(path, "rb") as fh:
                await update.message.reply_document(
                    document=fh, filename=fname, caption=caption)
        # (Ola 15 - B6, corregido en Ola 16) El backup manual se anota en
        # SU PROPIA clave: marcar `last_backup_ts` (la del automático)
        # escondía justo el fallo que /salud existe para detectar — el job
        # podía llevar días roto y /salud decir "hace 0 h" sin que
        # quedara ninguna copia en disco.
        def _marcar():
            from db import set_setting
            _c = get_conn()
            try:
                set_setting(_c, "last_backup_manual_ts", _t.time())
            finally:
                _c.close()
        await asyncio.to_thread(_marcar)
    except Exception as e:
        await update.message.reply_text(f"No pude generar el backup: {e}")
    finally:
        # (Ola 17-C) El borrado estaba dentro del try: si el envío fallaba
        # (base grande), el temporal se quedaba ahí para siempre.
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@solo_admin
async def cmd_ialocal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """IA local para salidas: /ialocal <url> · /ialocal on|off · /ialocal"""
    from db import set_setting, get_setting
    conn = get_conn()
    try:
        args = ctx.args or []
        if args:
            a = args[0].strip()
            if a.lower() == "off":
                set_setting(conn, "ia_local_activa", 0)
                await update.message.reply_text(
                    "🤖 IA local APAGADA: todo vuelve a reglas.")
                return
            if a.lower() == "proveedor":
                if len(args) > 1 and args[1].lower() in (
                        "local", "nube", "local_primero"):
                    set_setting(conn, "ia_proveedor", args[1].lower())
                    await update.message.reply_text(
                        f"🧠 Proveedor de IA: *{args[1].lower()}*",
                        parse_mode="Markdown")
                else:
                    actual = get_setting(conn, "ia_proveedor",
                                         "local_primero")
                    await update.message.reply_text(
                        f"🧠 Proveedor actual: {actual}\n"
                        "Cambiar: /ialocal proveedor local · "
                        "local_primero · nube")
                return
            if a.lower() == "on":
                url = get_setting(conn, "local_ai_url", "") or ""
                if not url:
                    await update.message.reply_text(
                        "Primero configura la URL: /ialocal https://…")
                    return
                set_setting(conn, "ia_local_activa", 1)
                await update.message.reply_text(
                    f"🤖 IA local ENCENDIDA · {url}\n"
                    "Mitad de las posiciones nuevas serán gestionadas "
                    "por la IA (A/B contra reglas).")
                return
            if a.startswith("http"):
                set_setting(conn, "local_ai_url", a.rstrip("/"))
                set_setting(conn, "ia_local_activa", 1)
                await update.message.reply_text(
                    f"🤖 URL guardada y experimento ENCENDIDO:\n{a}\n"
                    "Si el túnel cambia de URL, repite /ialocal <url>.")
                return
            await update.message.reply_text(
                "Uso: /ialocal <url> · /ialocal on · /ialocal off")
            return
        activa = get_setting(conn, "ia_local_activa", "0")
        url = get_setting(conn, "local_ai_url", "") or "(sin URL)"
        estado = "🟢 encendida" if str(activa) in ("1", "1.0") \
            else "🔴 apagada"
        await update.message.reply_text(
            f"🤖 IA local: {estado}\nURL: {url}\n"
            "Cambiar: /ialocal <url> · /ialocal on · /ialocal off")
    finally:
        conn.close()


@solo_admin
async def cmd_radar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/radar — qué vio el radar de pares recién nacidos en 24 h."""
    from radar import radar_text
    txt = await asyncio.to_thread(radar_text)
    await _send_md(update.message.chat, txt)     # (Ola 15 - M8) con fallback


@solo_admin
async def cmd_postmortem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/postmortem — decisiones vs resultado medido. 'ya' = regenerar."""
    fresco = bool(ctx.args and ctx.args[0].lower() in ("ya", "ahora"))
    if fresco:
        await update.message.reply_text(
            "⏳ Generando el post-mortem (la IA puede tardar un par de "
            "minutos)…")
    from post_mortem import post_mortem_text
    txt = await asyncio.to_thread(post_mortem_text, fresco)
    await _send_md(update.message.chat, txt)     # (Ola 15 - M8) con fallback


@solo_admin
async def cmd_salidas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deriva post-venta por billetera: ¿vende temprano o sale en la cima?"""
    def _calcular():                       # (Ola 15 - M5) fuera del loop
        from salidas import salidas_text, hold_report
        conn = get_conn()
        try:
            txt = salidas_text(conn)
            extra = hold_report(conn)
            return txt + ("\n" + extra if extra else "")
        finally:
            conn.close()
    txt = await asyncio.to_thread(_calcular)
    await _send_md(update.message.chat, txt)


@solo_admin
async def cmd_hermanas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 Buscando vínculos entre billeteras ⭐… (puede tardar ~1 min)")
    from wallet_links import find_links
    txt = await asyncio.to_thread(find_links)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _saldo_uno_text(addr: str) -> str:
    import requests as _rq
    try:
        resp = _rq.post(config.HELIUS_RPC,
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": "getBalance", "params": [addr]},
                        timeout=15)
        sol = resp.json()["result"]["value"] / 1e9
        return f"💰 Saldo de `{addr[:10]}…`: *{sol:,.2f} SOL*"
    except Exception:
        return "No pude consultar el saldo."


@solo_admin
async def cmd_saldos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Consultando saldos on-chain…")
    txt = await asyncio.to_thread(_saldos_text)
    await update.message.reply_text(txt, parse_mode="Markdown")


def _saldos_text():
    if True:
        import requests as _rq
        conn = get_conn()
        # (Ola 8, 21/8) Antes el encabezado decia "billeteras vigiladas" y
        # el pie "Total combinado" cuando la query cortaba a 25 de miles
        # (1.209 en la base el 21/8) y el total sumaba solo las consultadas
        # sin error. Ahora el corte y el alcance del total se dicen.
        poblacion = conn.execute(
            """SELECT COUNT(*) c FROM wallets
               WHERE is_tracked=1
                  OR (is_bot=0 AND winning_tokens_count >= 2)""").fetchone()["c"]
        rows = conn.execute(
            """SELECT address, alias, is_tracked FROM wallets
               WHERE is_tracked=1
                  OR (is_bot=0 AND winning_tokens_count >= 2)
               ORDER BY is_tracked DESC, score DESC LIMIT 25""").fetchall()
        conn.close()
        out = [f"💰 *Saldos — top {len(rows)} de {poblacion} vigiladas:*\n"]
        total = 0.0
        errores = 0
        for r in rows:
            try:
                resp = _rq.post(config.HELIUS_RPC,
                                json={"jsonrpc": "2.0", "id": 1,
                                      "method": "getBalance",
                                      "params": [r["address"]]},
                                timeout=15)
                sol = resp.json()["result"]["value"] / 1e9
            except Exception:
                sol = None
            nombre = _alias_md(r["alias"] or r["address"][:8])
            icono = "⭐" if r["is_tracked"] else "👁"
            if sol is None:
                errores += 1
                out.append(f"{icono} {nombre}: _error al consultar_")
            else:
                total += sol
                out.append(f"{icono} {nombre}: *{sol:,.2f} SOL*"
                           f"  `{r['address'][:8]}…`")
        consultadas = len(rows) - errores
        out.append(f"\nTotal de las {consultadas} consultadas: "
                   f"*{total:,.2f} SOL*"
                   + (f"  _({errores} con error)_" if errores else ""))

    return "\n".join(out)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Créalo con @BotFather.")
    if not ADMIN_ID:
        print("⚠️  TELEGRAM_ADMIN_ID no configurado: el bot responderá a "
              "CUALQUIERA. Configúralo antes de usarlo en serio.")
    # (Ola 15 - M6, acotado en Ola 16) concurrent_updates: sin esto PTB
    # procesa los updates EN FILA y una consulta al agente con la IA local
    # ocupada (~150 s) dejaba mudo al bot entero. Pero True = hasta 4096
    # updates a la vez, y cada handler abre SU conexión (db.get_conn no
    # tiene pool): una ráfaga agotaría el cupo de Postgres — el modo de
    # fallo que ya documenta realtime.py. Un tope de 8 da capacidad de
    # sobra para un solo usuario y acota las conexiones.
    app = (Application.builder().token(BOT_TOKEN)
           .concurrent_updates(8).post_init(_post_init).build())
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ciclo", cmd_ciclo))
    app.add_handler(CommandHandler("descubrir", cmd_descubrir))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("descartar", cmd_descartar))
    app.add_handler(CommandHandler("rastrear", cmd_rastrear))
    app.add_handler(CommandHandler("evidencia", cmd_evidencia))
    app.add_handler(CommandHandler("perfil", cmd_perfil))
    app.add_handler(CommandHandler("ficha", cmd_ficha))
    app.add_handler(CommandHandler("preguntar", cmd_preguntar))
    app.add_handler(CommandHandler("ia", cmd_ia))
    app.add_handler(CommandHandler("senales", cmd_senales))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rendimiento", cmd_rendimiento))
    app.add_handler(CommandHandler("estrellasperf", cmd_wallets_perf))
    app.add_handler(CommandHandler("salud", cmd_salud))
    app.add_handler(CommandHandler("datos", cmd_datos))
    app.add_handler(CommandHandler("reevaluar", cmd_reevaluar))
    app.add_handler(CommandHandler("exportar", cmd_exportar))
    app.add_handler(CommandHandler("errores", cmd_errores))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("ialocal", cmd_ialocal))
    app.add_handler(CommandHandler("salidas", cmd_salidas))
    app.add_handler(CommandHandler("postmortem", cmd_postmortem))
    app.add_handler(CommandHandler("radar", cmd_radar))
    app.add_handler(CommandHandler("hermanas", cmd_hermanas))
    app.add_handler(CommandHandler("adn", cmd_adn))
    app.add_handler(CommandHandler("clusters", cmd_clusters))
    app.add_handler(CommandHandler("prediccion", cmd_prediccion))
    app.add_handler(CommandHandler("lideres", cmd_lideres))
    app.add_handler(CommandHandler("predicciones", cmd_predicciones))
    app.add_handler(CommandHandler("metricas", cmd_metricas))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("elite", cmd_elite))
    app.add_handler(CommandHandler("alpha", cmd_alpha))
    app.add_handler(CommandHandler("atencion", cmd_atencion))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("similar", cmd_similar))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("estrellas", cmd_estrellas))
    app.add_handler(CommandHandler("hipotesis", cmd_hipotesis))
    app.add_handler(CommandHandler("entidad", cmd_entidad))
    app.add_handler(CommandHandler("entidades", cmd_entidad))
    app.add_handler(CommandHandler("saldos", cmd_saldos))
    app.add_handler(CommandHandler("paper", cmd_paper))
    app.add_handler(CommandHandler("topalertas", cmd_top_alertas))
    app.add_handler(CommandHandler("copiapura", cmd_copia_pura))
    app.add_handler(CommandHandler("reentrada", cmd_reentrada))
    app.add_handler(CommandHandler("nota", cmd_nota))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Chat libre: cualquier texto sin comando activa al agente
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_chat))

    # Servidor de webhooks para señales en tiempo real (Fase 2)
    start_webhook_server()
    # LaserStream corre EN PARALELO al webhook: menor latencia y, sobre todo,
    # reanuda desde el último slot tras un reinicio (antes esas señales se
    # perdían). No hay riesgo de alertas dobles: signals.signature es clave
    # primaria, así que la segunda vía que llegue se descarta sola.
    try:
        from laserstream import start as _ls_start
        if _ls_start():
            print("📡 LaserStream activo (webhook sigue como respaldo)")
    except Exception as e:
        print(f"· LaserStream no arrancó: {e}")

    # ── Reloj PERSISTENTE de jobs (Ola 5, auditoria 19/8 - M19) ─────
    # Los timers del job_queue viven en memoria y el supervisor local
    # reinicia el proceso con cada commit: cada deploy disparaba un
    # ciclo COMPLETO 60 s despues de arrancar (7 commits en un dia =
    # ~8 ciclos = purga acelerada de ⭐ + cuota de Helius/Gecko quemada),
    # y weekly_learning (first=3 dias) no corrio NUNCA. Ahora la ultima
    # ejecucion se guarda en settings y el `first` se calcula desde ahi:
    # un reinicio ya no adelanta nada, y un job vencido corre pronto.
    def _reloj_first(nombre: str, intervalo: int, defecto: int) -> int:
        try:
            from db import get_setting
            _c = get_conn()
            try:
                # (Ola 16) El más reciente de éxito e intento: un job que
                # falla no debe adelantar su próxima corrida en cada boot.
                last = max(
                    float(get_setting(_c, f"job_ts:{nombre}", 0) or 0),
                    float(get_setting(_c, f"job_intento:{nombre}", 0) or 0))
            finally:
                _c.close()
        except Exception:
            return defecto
        if not last:
            # Primera vez: se ancla el reloj YA, para que el tiempo de
            # calendario cuente ENTRE reinicios (sin esto, un job de
            # first largo — weekly_learning, 3 dias — necesitaria 3 dias
            # de proceso ininterrumpido y no corria jamas).
            try:
                from db import set_setting
                _c = get_conn()
                try:
                    set_setting(_c, f"job_ts:{nombre}",
                                _t.time() - intervalo + defecto)
                finally:
                    _c.close()
            except Exception:
                pass
            return defecto
        falta = intervalo - (_t.time() - last)
        if falta <= 0:                      # vencido: corre pronto
            return min(max(defecto, 60), 600)
        return int(min(falta, intervalo))

    # (Ola 17-B) Cada cuánto se COMPRUEBA si un job toca. Ver _con_reloj.
    _SONDEO_MAX = 1800

    def _con_reloj(nombre: str, fn, intervalo: int | None = None):
        # (Ola 17-B, auditoría 4) El reloj persistente calculaba bien
        # cuánto FALTABA, pero lo metía en un único `first` de horas o
        # días. El supervisor reinicia el bot con cada commit, y cada
        # reinicio volvía a programar ese `first` desde cero: un job
        # cuyo `first` es más largo que el tiempo entre reinicios NO
        # CORRE JAMÁS. Medido en la base del dueño: `post_mortem` con
        # `job_ts` de 140,8 h y SIN `job_intento` — o sea, ni un intento
        # en 6 días, cuando debía correr cada 7.
        # Ahora el job se registra con un sondeo corto y es este envoltorio
        # el que decide si toca. Un reinicio ya no reinicia nada: el
        # tiempo lo lleva la base, no el proceso.
        def _leer_reloj(nom):
            from db import get_setting
            _c0 = get_conn()
            try:
                return max(
                    float(get_setting(_c0, f"job_ts:{nom}", 0) or 0),
                    float(get_setting(_c0, f"job_intento:{nom}", 0) or 0))
            finally:
                _c0.close()

        async def _w(ctx):
            if intervalo:
                try:
                    # (Ola 17-E) En un HILO, igual que el marcado de tres
                    # lineas mas abajo: `get_conn()` en el bucle asincrono
                    # congela el bot mientras dura, y esto corre en cada
                    # tick de sondeo de los 7 jobs (con Postgres remoto,
                    # cada conexion son decenas de ms).
                    _last = await asyncio.to_thread(_leer_reloj, nombre)
                    if _last and (_t.time() - _last) < intervalo:
                        return                     # aún no toca
                except Exception:
                    pass       # si no se puede leer el reloj, se ejecuta
            # (Ola 15 - B3, corregido en Ola 16) DOS relojes:
            #   job_intento:<n>  → SIEMPRE, haya ido bien o mal.
            #   job_ts:<n>       → solo cuando el job terminó BIEN.
            # Con un solo reloj marcado en éxito, un auto_cycle que falla
            # (429 de Helius, Gecko caído) dejaba el reloj viejo y CADA
            # reinicio disparaba un ciclo completo a los 60 s — justo el
            # bug de la Ola 5 que el reloj persistente vino a cerrar.
            # `_reloj_first` usa el más reciente de los dos, así que un
            # fallo ya no adelanta nada, y el éxito sigue siendo lo que
            # marca el ritmo real.
            # (Ola 17-B) Antes, 6 de los 7 jobs envueltos se tragaban su
            # propia excepción, así que `_ok` era SIEMPRE True y `job_ts`
            # se marcaba "terminó bien" aunque el backup o el aprendizaje
            # hubieran reventado por dentro. Ahora esos jobs propagan el
            # fallo (`raise`) y aquí se captura: el reloj de éxito no se
            # marca, y la excepción no sale al job_queue.
            _ok, _err = False, None
            try:
                await fn(ctx)
                _ok = True
            except Exception as e:
                _err = e
            finally:
                # El marcado va a un HILO: `get_conn()` en el bucle
                # asíncrono congela el bot entero mientras dura, y esto
                # corre en cada tick de los 7 jobs, falle o no.
                def _marcar(ok):
                    try:
                        from db import set_setting
                        _c = get_conn()
                        try:
                            set_setting(_c, f"job_intento:{nombre}",
                                        _t.time())
                            if ok:
                                set_setting(_c, f"job_ts:{nombre}",
                                            _t.time())
                        finally:
                            _c.close()
                    except Exception:
                        pass
                try:
                    await asyncio.to_thread(_marcar, _ok)
                except Exception:
                    pass
                if not _ok:
                    print(f"· {nombre}: falló ({_err}) — el reloj de éxito "
                          f"no se marca; el ritmo lo fija el intento")
                    try:
                        from errores import record as _rec
                        await asyncio.to_thread(_rec, f"job:{nombre}", _err)
                    except Exception:
                        pass
        return _w

    # Ciclo automático: cada N horas DE VERDAD (reloj persistente)
    _iv = int(AUTO_CYCLE_HOURS * 3600)
    app.job_queue.run_repeating(
        _con_reloj("auto_cycle", auto_cycle_job, _iv),
        interval=min(_iv, _SONDEO_MAX),
        first=min(_reloj_first("auto_cycle", _iv, 60), _SONDEO_MAX),
        name="auto_cycle",
    )
    # (Ola 17-J) Rescate del precio de entrada: cada 2 min, ventana corta
    app.job_queue.run_repeating(
        rescate_precios_job,
        interval=120,
        first=90,
        name="rescate_precios",
    )
    # Track record: mide el resultado de las señales cada 15 min
    app.job_queue.run_repeating(
        track_outcomes_job,
        interval=900,
        first=120,
        name="track_outcomes",
    )
    # Paper trading: revisa TP/SL/tiempo de las simuladas cada 15 min
    app.job_queue.run_repeating(
        paper_job,
        interval=900,
        first=300,
        name="paper_trading",
    )
    # (Ola 18-C) Ventana fría del arranque: hasta que los dos grafos
    # estén en caché, `on_buy` no abre ninguna predicción, y esas compras
    # no se recuperan. `predictions_job` no llega hasta el minuto 6, así
    # que se precalienta una vez a los 40 s. Va en su propio hilo: no
    # retrasa el arranque ni bloquea la ingesta.
    app.job_queue.run_once(warmup_job, when=40, name="warmup_grafos")
    # Motor predictivo: evalúa predicciones vencidas cada 10 min
    app.job_queue.run_repeating(
        predictions_job,
        interval=600,
        first=360,
        name="predictions_eval",
    )
    # Motor de hipótesis: descubrimiento autónomo cada 12 h
    app.job_queue.run_repeating(
        _con_reloj("hypotheses", hypotheses_job, 12 * 3600),
        interval=min(12 * 3600, _SONDEO_MAX),
        first=min(_reloj_first("hypotheses", 12 * 3600, 1800), _SONDEO_MAX),
        name="hypotheses",
    )
    # Backup diario de la base + watchdog del webhook + aprendizaje semanal
    app.job_queue.run_repeating(
        _con_reloj("db_backup", backup_job, 86400),
        interval=min(86400, _SONDEO_MAX),
        first=min(_reloj_first("db_backup", 86400, 7200), _SONDEO_MAX),
        name="db_backup")
    app.job_queue.run_repeating(watchdog_job, interval=3600, first=1800,
                                name="watchdog")
    app.job_queue.run_repeating(
        _con_reloj("weekly_learning", learning_job, 7 * 86400),
        interval=min(7 * 86400, _SONDEO_MAX),
        first=min(_reloj_first("weekly_learning", 7 * 86400, 3 * 86400),
                  _SONDEO_MAX),
        name="weekly_learning")
    # Radar de pares recién nacidos (Ola 14): cada 15 min
    app.job_queue.run_repeating(radar_job, interval=900, first=600,
                                name="radar")
    # Post-mortem (Ola 11): la IA revisa sus decisiones cada 7 días
    app.job_queue.run_repeating(
        _con_reloj("post_mortem", post_mortem_job, 7 * 86400),
        interval=min(7 * 86400, _SONDEO_MAX),
        first=min(_reloj_first("post_mortem", 7 * 86400, 4 * 86400),
                  _SONDEO_MAX),
        name="post_mortem")
    # Cierre del ciclo: el rendimiento medido degrada ⭐ cada 24 h
    app.job_queue.run_repeating(
        _con_reloj("performance_review", performance_review_job, 86400),
        interval=min(86400, _SONDEO_MAX),
        first=min(_reloj_first("performance_review", 86400, 3600),
                  _SONDEO_MAX),
        name="performance_review")
    # Autodiagnóstico cada 6 h (solo avisa ante problemas críticos)
    app.job_queue.run_repeating(
        _con_reloj("salud", salud_job, 6 * 3600),
        interval=min(6 * 3600, _SONDEO_MAX),
        first=min(_reloj_first("salud", 6 * 3600, 900), _SONDEO_MAX),
        name="salud")
    # Re-sincroniza el webhook con las ⭐ cada 30 min (nadie sin monitorear)
    app.job_queue.run_repeating(sync_webhook_job, interval=1800, first=300,
                                name="sync_webhook")
    # Resumen diario a las 13:00 UTC (~8am América)
    import datetime as _dt
    _now = _dt.datetime.now(_dt.timezone.utc)
    _target = _now.replace(hour=13, minute=0, second=0, microsecond=0)
    if _target <= _now:
        _target += _dt.timedelta(days=1)
    app.job_queue.run_repeating(daily_summary_job, interval=86400,
                                first=(_target - _now).total_seconds(),
                                name="daily_summary")

    print(f"🤖 Bot corriendo. Ciclo automático cada {AUTO_CYCLE_HOURS:g} h.")
    app.run_polling()


if __name__ == "__main__":
    main()
