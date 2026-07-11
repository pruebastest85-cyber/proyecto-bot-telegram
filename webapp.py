"""
Mini App de Telegram: panel visual del sistema.

Se abre con /app dentro del chat del bot. Sirve un dashboard oscuro con
las estadísticas, el top de billeteras (score, PnL, botón descartar) y
las últimas señales con sus resultados.

Seguridad: cada llamada a la API valida el initData firmado por
Telegram (HMAC con el token del bot) y que el usuario sea el admin.
"""

import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl

from flask import request, jsonify, Response

from db import get_conn, get_setting, top_wallets

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")


def _valid_init_data(init_data: str) -> bool:
    """Verifica la firma de Telegram y que el usuario sea el admin."""
    if not init_data or not BOT_TOKEN:
        return False
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        recibido = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(),
                          hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recibido):
            return False
        user = json.loads(data.get("user", "{}"))
        return not ADMIN_ID or str(user.get("id")) == str(ADMIN_ID)
    except Exception:
        return False


def register_webapp(app):
    @app.get("/app")
    def webapp_page():
        return Response(PAGE, mimetype="text/html")

    @app.post("/api/overview")
    def api_overview():
        body = request.get_json(silent=True) or {}
        if not _valid_init_data(body.get("initData", "")):
            return jsonify({"error": "unauthorized"}), 401
        conn = get_conn()
        stats = {
            "tokens": conn.execute(
                "SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"],
            "pendientes": conn.execute(
                "SELECT COUNT(*) c FROM winning_tokens WHERE analyzed=0"
            ).fetchone()["c"],
            "billeteras": conn.execute(
                "SELECT COUNT(*) c FROM wallets").fetchone()["c"],
            "rastreadas": conn.execute(
                "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1"
            ).fetchone()["c"],
            "descartadas": conn.execute(
                "SELECT COUNT(*) c FROM wallets WHERE is_bot=1"
            ).fetchone()["c"],
            "senales": conn.execute(
                "SELECT COUNT(*) c FROM signals").fetchone()["c"],
            "umbral": float(get_setting(conn, "min_signal_score", "0") or 0),
        }
        wallets = [dict(r) for r in top_wallets(conn, 20)]
        senales = [dict(r) for r in conn.execute(
            """SELECT s.symbol, s.mint, s.side, s.sol, s.ts, s.chg_1h,
                      s.chg_24h, s.signal_score, w.alias
               FROM signals s LEFT JOIN wallets w ON w.address=s.wallet
               ORDER BY s.ts DESC LIMIT 15""").fetchall()]
        conn.close()
        return jsonify({"stats": stats, "wallets": wallets,
                        "senales": senales})

    @app.post("/api/discard")
    def api_discard():
        body = request.get_json(silent=True) or {}
        if not _valid_init_data(body.get("initData", "")):
            return jsonify({"error": "unauthorized"}), 401
        addr = (body.get("address") or "").strip()
        if not addr:
            return jsonify({"error": "falta address"}), 400
        from wallet_admin import discard_wallet
        return jsonify({"msg": discard_wallet(addr)})


PAGE = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wallet Tracker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0e1117;--card:#171b24;--line:#232936;--tx:#e6e9ef;--mut:#8b93a7;
--acc:#7c5cff;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,system-ui,
Segoe UI,Roboto,sans-serif;padding:14px 12px 40px}
h1{font-size:18px;margin-bottom:2px}
.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px;text-align:center}
.stat b{font-size:18px;display:block}
.stat span{font-size:10px;color:var(--mut);text-transform:uppercase}
h2{font-size:14px;margin:14px 0 8px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px 12px;margin-bottom:8px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.alias{font-weight:600;font-size:14px}
.addr{color:var(--mut);font-size:11px;word-break:break-all}
.meta{color:var(--mut);font-size:12px;margin-top:3px}
.score{min-width:44px;text-align:center;border-radius:10px;padding:5px 6px;
font-weight:700;font-size:13px}
.s-hi{background:rgba(34,197,94,.15);color:var(--ok)}
.s-md{background:rgba(245,158,11,.15);color:var(--warn)}
.s-lo{background:rgba(239,68,68,.15);color:var(--bad)}
.btn{background:rgba(239,68,68,.12);color:var(--bad);border:none;
border-radius:10px;padding:7px 10px;font-size:12px;font-weight:600}
.chip{display:inline-block;border-radius:8px;padding:2px 7px;font-size:11px;
font-weight:600;margin-left:4px}
.up{background:rgba(34,197,94,.15);color:var(--ok)}
.dn{background:rgba(239,68,68,.15);color:var(--bad)}
.buy{color:var(--ok)}.sell{color:var(--bad)}
.load{color:var(--mut);text-align:center;padding:30px 0}
.star{color:#fbbf24}
</style></head><body>
<h1>📡 Wallet Tracker</h1>
<div class="sub" id="sub">Cargando…</div>
<div class="grid" id="stats"></div>
<h2>🏆 Top billeteras</h2><div id="wallets" class="load">Cargando…</div>
<h2>📊 Últimas señales</h2><div id="senales" class="load">Cargando…</div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const $=id=>document.getElementById(id);
function scoreCls(s){return s>=70?'s-hi':(s>=45?'s-md':'s-lo')}
function fmtMc(v){if(!v)return'?';return v>=1e6?'$'+(v/1e6).toFixed(1)+'M':'$'+(v/1e3).toFixed(0)+'K'}
async function api(path,extra){const r=await fetch(path,{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify(Object.assign({initData:tg.initData},extra||{}))});
if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
function render(d){
const s=d.stats;
$('sub').textContent='Umbral de señal: '+s.umbral.toFixed(0)+'/100';
$('stats').innerHTML=[['Rastreadas ⭐',s.rastreadas],['Billeteras',s.billeteras],
['Señales',s.senales],['Tokens',s.tokens],['En cola',s.pendientes],
['Bots ❌',s.descartadas]].map(x=>'<div class="stat"><b>'+x[1]+'</b><span>'+
x[0]+'</span></div>').join('');
$('wallets').className='';
$('wallets').innerHTML=d.wallets.length?d.wallets.map(w=>{
const ws=w.wallet_score!=null?Math.round(w.wallet_score):null;
const pnl=[];if(w.pnl_30d!=null)pnl.push('30d: '+w.pnl_30d.toFixed(1));
if(w.pnl_total!=null)pnl.push('hist: '+w.pnl_total.toFixed(1));
return '<div class="card"><div class="row"><div style="flex:1">'+
'<div class="alias">'+(w.is_tracked?'<span class="star">★</span> ':'')+
(w.alias||'Sin alias')+(w.ai_class?' <span class="chip" style="background:#232936;color:#8b93a7">'+w.ai_class+'</span>':'')+'</div>'+
'<div class="addr">'+w.address+'</div>'+
'<div class="meta">ganadores: '+w.winning_tokens_count+
(pnl.length?' · PnL(SOL) '+pnl.join(' · '):'')+'</div></div>'+
(ws!=null?'<div class="score '+scoreCls(ws)+'">'+ws+'</div>':'')+
'<button class="btn" onclick="descartar(\\''+w.address+'\\')">✕</button>'+
'</div></div>'}).join(''):'<div class="load">Aún no hay billeteras</div>';
$('senales').className='';
$('senales').innerHTML=d.senales.length?d.senales.map(x=>{
const hace=((Date.now()/1000-x.ts)/3600).toFixed(1);
const chips=[];
if(x.chg_1h!=null)chips.push('<span class="chip '+(x.chg_1h>=0?'up':'dn')+'">1h '+x.chg_1h.toFixed(0)+'%</span>');
if(x.chg_24h!=null)chips.push('<span class="chip '+(x.chg_24h>=0?'up':'dn')+'">24h '+x.chg_24h.toFixed(0)+'%</span>');
return '<div class="card"><div class="row"><div style="flex:1">'+
'<span class="'+(x.side==='compra'?'buy':'sell')+'">'+(x.side==='compra'?'🟢':'🔴')+
' <b>'+(x.symbol||x.mint.slice(0,8))+'</b></span>'+
'<span class="meta"> '+x.sol.toFixed(1)+' SOL · '+(x.alias||'')+' · hace '+hace+'h</span>'+
'<div style="margin-top:4px">'+chips.join(' ')+'</div></div>'+
(x.signal_score!=null?'<div class="score '+scoreCls(x.signal_score)+'">'+Math.round(x.signal_score)+'</div>':'')+
'</div></div>'}).join(''):'<div class="load">Aún no hay señales</div>';
}
async function descartar(addr){
tg.showConfirm('¿Descartar la billetera '+addr.slice(0,8)+'…? Dejará de rastrearse.',async ok=>{
if(!ok)return;try{const r=await api('/api/discard',{address:addr});
tg.showAlert(r.msg||'Hecho');cargar()}catch(e){tg.showAlert('Error: '+e.message)}})}
async function cargar(){try{render(await api('/api/overview'))}
catch(e){$('sub').textContent='Error cargando datos ('+e.message+'). ¿Abriste desde el botón del bot?'}}
cargar();
</script></body></html>"""
