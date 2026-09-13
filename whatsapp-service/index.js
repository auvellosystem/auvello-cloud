import express from "express";
import fs from "node:fs";
import { Pool } from "pg";
import qrcodeTerminal from "qrcode-terminal";
import QRCode from "qrcode";
import pino from "pino";
import makeWASocket, { DisconnectReason, useMultiFileAuthState } from "@whiskeysockets/baileys";
import { useNeonAuthState } from "./neon-auth.js";
import { Boom } from "@hapi/boom";

const app = express();
app.use(express.json({ limit: "1mb" }));

let sock = null;
let ready = false;
let latestQr = null;
const communityRate = new Map();

const ADMIN_GROUPS = {
  eletronicos_tecnologia: "Eletrônicos e Tecnologia",
  moda_vestuario: "Moda e Vestuário",
  celulares_acessorios: "Celulares e Acessórios",
  games_acessorios: "Games e Acessórios",
  utilidades_domesticas: "Utilidades Domésticas",
  pet_shop: "Pet Shop"
};

const databaseUrl = process.env.DATABASE_URL?.trim();
const adminPool = databaseUrl ? new Pool({ connectionString: databaseUrl }) : null;
let adminTableReady = false;

async function ensureAdminTable() {
  if (!adminPool) throw new Error("DATABASE_URL não configurada.");
  if (adminTableReady) return;
  await adminPool.query(`
    CREATE TABLE IF NOT EXISTS admin_monitored_products (
      id BIGSERIAL PRIMARY KEY,
      product_id TEXT NOT NULL UNIQUE,
      group_key TEXT NOT NULL,
      active BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await adminPool.query(`
    CREATE INDEX IF NOT EXISTS idx_admin_monitored_products_active
    ON admin_monitored_products(active, group_key)
  `);
  await adminPool.query(`
    CREATE TABLE IF NOT EXISTS community_requests (
      id BIGSERIAL PRIMARY KEY,
      name TEXT NOT NULL,
      whatsapp TEXT NOT NULL,
      reference_url TEXT NOT NULL,
      reference_product_id TEXT,
      desired_item TEXT NOT NULL,
      suggested_group_key TEXT NOT NULL,
      approved_group_key TEXT,
      approved_search_term TEXT,
      notes TEXT,
      status TEXT NOT NULL DEFAULT 'pendente',
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await adminPool.query(`
    CREATE INDEX IF NOT EXISTS idx_community_requests_status
    ON community_requests(status, approved_group_key, created_at)
  `);
  adminTableReady = true;
}

function adminAuth(req, res, next) {
  const expectedUser = process.env.ADMIN_USER?.trim();
  const expectedPassword = process.env.ADMIN_PASSWORD || "";
  if (!expectedUser || !expectedPassword) {
    return res.status(503).send("Auvello Admin desativado. Configure ADMIN_USER e ADMIN_PASSWORD no Render.");
  }

  const auth = req.headers.authorization || "";
  if (!auth.startsWith("Basic ")) {
    res.set("WWW-Authenticate", 'Basic realm="Auvello Admin", charset="UTF-8"');
    return res.status(401).send("Autenticação necessária.");
  }

  let decoded = "";
  try {
    decoded = Buffer.from(auth.slice(6), "base64").toString("utf8");
  } catch (_error) {
    decoded = "";
  }
  const separator = decoded.indexOf(":");
  const user = separator >= 0 ? decoded.slice(0, separator) : "";
  const password = separator >= 0 ? decoded.slice(separator + 1) : "";

  if (user !== expectedUser || password !== expectedPassword) {
    res.set("WWW-Authenticate", 'Basic realm="Auvello Admin", charset="UTF-8"');
    return res.status(401).send("Usuário ou senha inválidos.");
  }
  next();
}

function extractProductId(value) {
  const text = String(value || "").trim().toUpperCase();
  const direct = text.match(/^MLB\d+$/);
  if (direct) return direct[0];
  const fromCatalogUrl = text.match(/\/P\/(MLB\d+)/);
  if (fromCatalogUrl) return fromCatalogUrl[1];
  const anywhere = text.match(/\b(MLB\d{5,})\b/);
  return anywhere ? anywhere[1] : null;
}

function normalizeText(value, maxLength = 300) {
  return String(value || "").trim().replace(/\s+/g, " ").slice(0, maxLength);
}

function normalizeWhatsapp(value) {
  return String(value || "").replace(/[^0-9+()\-\s]/g, "").trim().slice(0, 30);
}

function validateMercadoLivreUrl(value) {
  const raw = String(value || "").trim();
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    const validHost = host === "mercadolivre.com.br" || host.endsWith(".mercadolivre.com.br") || host === "mercadolibre.com" || host.endsWith(".mercadolibre.com");
    if (!validHost || !["http:", "https:"].includes(url.protocol)) return null;
    return url.toString().slice(0, 2000);
  } catch (_error) {
    return null;
  }
}

function communityRateAllowed(req) {
  const now = Date.now();
  const windowMs = 60 * 60 * 1000;
  const limit = 5;
  const key = String(req.headers["x-forwarded-for"] || req.socket.remoteAddress || "unknown").split(",")[0].trim();
  const recent = (communityRate.get(key) || []).filter(ts => now - ts < windowMs);
  if (recent.length >= limit) return false;
  recent.push(now);
  communityRate.set(key, recent);
  return true;
}

function isInStaticWatchlist(productId) {
  try {
    const raw = fs.readFileSync("watchlist.json", "utf8");
    const data = JSON.parse(raw);
    const ids = Array.isArray(data.product_identifiers) ? data.product_identifiers : [];
    return ids.some((id) => String(id).trim().toUpperCase() === productId);
  } catch (_error) {
    return false;
  }
}

app.get("/pedir-oferta", (_req, res) => {
  res.status(200).type("html").send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Pedir uma oferta - Auvello</title>
  <style>
    :root{color-scheme:dark;--bg:#090d0b;--panel:#111815;--green:#36e676;--text:#f3f7f4;--muted:#93a69b;--border:#26362e;--warn:#d8b85a}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at top,#183023 0,#090d0b 42%);font-family:Inter,Arial,sans-serif;color:var(--text)}
    .wrap{max-width:760px;margin:0 auto;padding:30px 18px 70px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:24px}.logo{width:52px;height:52px;border-radius:15px;background:linear-gradient(145deg,#48f98a,#168c48);display:grid;place-items:center;color:#07120b;font-size:27px;font-weight:900;box-shadow:0 0 35px #35e67638}.brand h1{margin:0;font-size:25px}.brand p{margin:4px 0 0;color:var(--muted)}
    .card{background:#111815ee;border:1px solid var(--border);border-radius:20px;padding:22px;box-shadow:0 20px 65px #0006}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;font-size:13px;font-weight:750;margin-bottom:7px}.help{font-size:12px;color:var(--muted);line-height:1.45;margin-top:6px}input,select,textarea,button{width:100%;border-radius:11px;border:1px solid var(--border);font:inherit}input,select,textarea{background:#0c120f;color:var(--text);padding:12px 13px;outline:none}input,select{height:46px}textarea{min-height:92px;resize:vertical}input:focus,select:focus,textarea:focus{border-color:var(--green)}button{height:50px;background:var(--green);color:#06200f;font-weight:900;cursor:pointer;border:none;margin-top:5px}.notice{padding:14px 15px;border-radius:13px;background:#171b13;border:1px solid #4b4524;color:#e8e2c8;font-size:13px;line-height:1.55}.notice strong{color:#f2ce65}.msg{min-height:22px;margin-top:13px;font-size:14px;color:var(--muted)}.msg.ok{color:var(--green)}.msg.err{color:#ff9090}.honeypot{position:absolute;left:-10000px;opacity:0;pointer-events:none}
    @media(max-width:650px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.wrap{padding-top:20px}.brand p{font-size:13px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="logo">A</div><div><h1>Ajude o Auvello a buscar melhor</h1><p>Envie um interesse de oferta para análise.</p></div></div>
  <div class="card">
    <div class="grid">
      <div><label for="name">Nome</label><input id="name" maxlength="80" autocomplete="name" placeholder="Seu nome"></div>
      <div><label for="whatsapp">WhatsApp para contato</label><input id="whatsapp" maxlength="30" inputmode="tel" autocomplete="tel" placeholder="(27) 99999-9999"><div class="help"><strong>Usaremos este número apenas se precisarmos esclarecer ou dar retorno sobre sua solicitação.</strong></div></div>
      <div class="full"><label for="referenceUrl">Link de referência do Mercado Livre</label><input id="referenceUrl" maxlength="2000" inputmode="url" placeholder="https://www.mercadolivre.com.br/..."><div class="help">O link serve para entendermos melhor o tipo de produto desejado.</div></div>
      <div class="full"><label for="desiredItem">O que você gostaria que o Auvello buscasse?</label><input id="desiredItem" maxlength="180" placeholder="Ex.: ração para gatos adultos, controle PS5, Air Fryer 5L"></div>
      <div><label for="groupKey">Grupo sugerido</label><select id="groupKey">${Object.entries(ADMIN_GROUPS).map(([key,label]) => `<option value="${key}">${label}</option>`).join("")}</select><div class="help">É apenas uma sugestão. O Auvello pode ajustar o grupo após análise.</div></div>
      <div><label for="notes">Observação (opcional)</label><textarea id="notes" maxlength="500" placeholder="Ex.: prefiro pacote de 10 kg ou mais"></textarea></div>
      <div class="honeypot" aria-hidden="true"><label>Empresa</label><input id="company" tabindex="-1" autocomplete="off"></div>
      <div class="full notice"><strong>Importante:</strong> o link enviado será utilizado como referência para entendermos o tipo/categoria de produto que você procura. O envio desta solicitação não significa que esse anúncio específico será monitorado ou publicado. Todas as solicitações passam por análise de viabilidade e enquadramento no grupo adequado.</div>
      <div class="full"><button id="send">ENVIAR SOLICITAÇÃO</button><div id="message" class="msg"></div></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const msg=(text,kind='')=>{$('message').textContent=text;$('message').className='msg '+kind;};
$('send').addEventListener('click', async()=>{
  const payload={name:$('name').value.trim(),whatsapp:$('whatsapp').value.trim(),referenceUrl:$('referenceUrl').value.trim(),desiredItem:$('desiredItem').value.trim(),groupKey:$('groupKey').value,notes:$('notes').value.trim(),company:$('company').value};
  if(!payload.name||!payload.whatsapp||!payload.referenceUrl||!payload.desiredItem)return msg('Preencha nome, WhatsApp, link de referência e o que deseja encontrar.','err');
  $('send').disabled=true;msg('Enviando...');
  try{
    const res=await fetch('/api/community/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await res.json().catch(()=>({}));
    if(!res.ok)throw new Error(data.error||'Não foi possível enviar.');
    msg('Solicitação enviada! Ela ficará pendente até a análise do Auvello.','ok');
    $('referenceUrl').value='';$('desiredItem').value='';$('notes').value='';
  }catch(e){msg(e.message,'err');}finally{$('send').disabled=false;}
});
</script>
</body></html>`);
});

app.post("/api/community/requests", async (req, res) => {
  if (!communityRateAllowed(req)) return res.status(429).json({ error: "Muitas solicitações em pouco tempo. Tente novamente mais tarde." });
  if (req.body?.company) return res.status(201).json({ ok: true });

  const name = normalizeText(req.body?.name, 80);
  const whatsapp = normalizeWhatsapp(req.body?.whatsapp);
  const referenceUrl = validateMercadoLivreUrl(req.body?.referenceUrl);
  const desiredItem = normalizeText(req.body?.desiredItem, 180);
  const suggestedGroupKey = String(req.body?.groupKey || "").trim();
  const notes = normalizeText(req.body?.notes, 500) || null;

  if (name.length < 2) return res.status(400).json({ error: "Informe seu nome." });
  if (whatsapp.replace(/\D/g, "").length < 10) return res.status(400).json({ error: "Informe um WhatsApp válido para contato." });
  if (!referenceUrl) return res.status(400).json({ error: "Informe um link válido do Mercado Livre." });
  if (desiredItem.length < 3) return res.status(400).json({ error: "Descreva o que você gostaria que o Auvello buscasse." });
  if (!ADMIN_GROUPS[suggestedGroupKey]) return res.status(400).json({ error: "Grupo sugerido inválido." });

  try {
    await ensureAdminTable();
    const referenceProductId = extractProductId(referenceUrl);
    const { rows } = await adminPool.query(
      `INSERT INTO community_requests
       (name, whatsapp, reference_url, reference_product_id, desired_item, suggested_group_key, notes, status, created_at, updated_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,'pendente',NOW(),NOW())
       RETURNING id, status, created_at`,
      [name, whatsapp, referenceUrl, referenceProductId, desiredItem, suggestedGroupKey, notes]
    );
    console.log(`[community] nova solicitação #${rows[0].id}: ${desiredItem} -> ${suggestedGroupKey}`);
    res.status(201).json({ ok: true, request: rows[0] });
  } catch (error) {
    console.error("[community] create:", error);
    res.status(500).json({ error: "Não foi possível registrar sua solicitação." });
  }
});

app.get("/admin", adminAuth, (_req, res) => {
  res.status(200).type("html").send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Auvello Admin</title>
  <style>
    :root{color-scheme:dark;--bg:#090d0b;--panel:#111815;--panel2:#17201c;--green:#36e676;--text:#f3f7f4;--muted:#8fa298;--danger:#ff6b6b;--border:#26362e;--yellow:#e8c65c;--blue:#74b9ff}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#14231b 0,#090d0b 40%);font-family:Inter,Arial,sans-serif;color:var(--text);min-height:100vh}.wrap{max-width:1180px;margin:0 auto;padding:28px 18px 70px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:24px}.logo{width:48px;height:48px;border-radius:14px;background:linear-gradient(145deg,#48f98a,#168c48);display:grid;place-items:center;color:#07120b;font-size:25px;font-weight:900;box-shadow:0 0 35px #35e67638}.brand h1{margin:0;font-size:24px}.brand p{margin:4px 0 0;color:var(--muted)}
    .card{background:#111815e8;border:1px solid var(--border);border-radius:18px;padding:20px;box-shadow:0 18px 60px #0005;margin-bottom:18px}h2{font-size:17px;margin:0 0 16px}.form{display:grid;grid-template-columns:1.5fr 1fr auto;gap:10px}input,select,textarea,button{border-radius:11px;border:1px solid var(--border);font:inherit}input,select,textarea{background:#0c120f;color:var(--text);padding:0 13px;outline:none}input,select{height:46px}textarea{padding:10px 13px;min-height:70px}input:focus,select:focus,textarea:focus{border-color:var(--green)}button{height:46px;padding:0 17px;background:var(--green);color:#06200f;font-weight:800;cursor:pointer;border:none}button.secondary{background:#25332c;color:var(--text)}button.danger{background:#321b1b;color:#ffaaaa;border:1px solid #5e2b2b}button.warn{background:#4b4020;color:#f2d877}.msg{margin-top:12px;min-height:20px;color:var(--muted);font-size:14px}.msg.ok{color:var(--green)}.msg.err{color:#ff8d8d}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:820px}th,td{text-align:left;padding:13px 10px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.pill{display:inline-block;padding:5px 9px;border-radius:99px;font-size:12px;font-weight:700;background:#183425;color:#65f49c}.pill.off{background:#332828;color:#c7aaa9}.pill.pending{background:#3a331c;color:#f0cf69}.pill.analysis{background:#1d3140;color:#86c9ff}.pill.contact{background:#402c1c;color:#ffbd7a}.pill.rejected{background:#3a2020;color:#ff9999}.actions{display:flex;gap:7px;flex-wrap:wrap}.actions button{height:34px;padding:0 10px;font-size:12px}.empty{color:var(--muted);padding:18px 0}.hint{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.5}.topline{display:flex;align-items:center;justify-content:space-between;gap:12px}.refresh{height:36px!important;background:#25332c!important;color:var(--text)!important}.request{border:1px solid var(--border);border-radius:15px;padding:15px;margin:11px 0;background:#0d1410}.request-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.request h3{margin:0 0 5px;font-size:16px}.meta{color:var(--muted);font-size:12px;line-height:1.5}.request-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:13px}.request-grid label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}.request-grid input,.request-grid select{width:100%;height:40px}.ref{color:#7ceca5;text-decoration:none;word-break:break-all}.note{margin-top:10px;font-size:13px;color:#c8d2cc}.community-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.community-actions button{height:36px;font-size:12px}.wa{color:#72eda0;text-decoration:none}
    @media(max-width:720px){.form{grid-template-columns:1fr}.form button{width:100%}.wrap{padding-top:18px}.brand p{font-size:13px}.request-grid{grid-template-columns:1fr}.request-head{display:block}.request-head .pill{margin-top:7px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="logo">A</div><div><h1>Auvello Admin</h1><p>Produtos fixados e pedidos da comunidade</p></div></div>

  <section class="card">
    <h2>Adicionar produto ao monitoramento permanente</h2>
    <div class="form">
      <input id="product" autocomplete="off" placeholder="MLB29089153 ou link completo do Mercado Livre">
      <select id="group">${Object.entries(ADMIN_GROUPS).map(([key,label]) => `<option value="${key}">${label}</option>`).join("")}</select>
      <button id="add">+ ADICIONAR</button>
    </div>
    <div class="hint">O cadastro não força publicação. O produto será consultado em todas as rodadas e ainda precisa passar pelas regras atuais de desconto, score, slots e cooldown. O grupo Geral continua automático e não é selecionável aqui.</div>
    <div id="message" class="msg"></div>
  </section>

  <section class="card">
    <div class="topline"><h2>Produtos fixados</h2><button class="refresh" id="refresh">Atualizar</button></div>
    <div class="tablewrap"><div id="content" class="empty">Carregando...</div></div>
  </section>

  <section class="card">
    <div class="topline"><div><h2>Pedidos da Comunidade</h2><div class="hint" style="margin-top:-8px">O link é só referência. Ao aprovar, confirme o termo/categoria de busca e o grupo correto.</div></div><button class="refresh" id="refreshRequests">Atualizar</button></div>
    <div id="requests" class="empty">Carregando...</div>
  </section>
</div>
<script>
const GROUPS=${JSON.stringify(ADMIN_GROUPS)};
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const message=(text,kind='')=>{$('message').textContent=text;$('message').className='msg '+kind;};
const fmtDate=v=>v?new Date(v).toLocaleString('pt-BR'):'—';
const money=v=>v==null?'—':Number(v).toLocaleString('pt-BR',{style:'currency',currency:'BRL'});
async function api(url,options={}){const res=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const data=await res.json().catch(()=>({}));if(!res.ok){const err=new Error(data.error||'Erro na requisição');err.status=res.status;err.data=data;throw err;}return data;}

async function load(){try{const data=await api('/api/admin/products');if(!data.products.length){$('content').innerHTML='<div class="empty">Nenhum produto fixado ainda.</div>';return;}const rows=data.products.map(p=>'<tr><td><strong>'+esc(p.product_id)+'</strong>'+(p.in_static_watchlist?'<br><small style="color:#d7b85b">Também está no watchlist.json</small>':'')+'</td><td>'+esc(GROUPS[p.group_key]||p.group_key)+'</td><td><span class="pill '+(p.active?'':'off')+'">'+(p.active?'ATIVO':'PAUSADO')+'</span></td><td>'+(p.last_price==null?'—':money(p.last_price))+(p.last_discount==null?'':'<br><small>'+Number(p.last_discount).toFixed(1)+'% OFF</small>')+'</td><td>'+fmtDate(p.last_sent_at)+'</td><td>'+fmtDate(p.created_at)+'</td><td class="actions"><button class="secondary" data-action="toggle" data-product="'+esc(p.product_id)+'" data-active="'+(!p.active)+'">'+(p.active?'Pausar':'Ativar')+'</button><button class="danger" data-action="remove" data-product="'+esc(p.product_id)+'">Excluir</button></td></tr>').join('');$('content').innerHTML='<table><thead><tr><th>Produto</th><th>Grupo</th><th>Status</th><th>Última publicação</th><th>Publicado em</th><th>Adicionado</th><th>Ações</th></tr></thead><tbody>'+rows+'</tbody></table>';$('content').querySelectorAll('[data-action="toggle"]').forEach(btn=>btn.addEventListener('click',()=>toggleProduct(btn.dataset.product,btn.dataset.active==='true')));$('content').querySelectorAll('[data-action="remove"]').forEach(btn=>btn.addEventListener('click',()=>removeProduct(btn.dataset.product)));}catch(e){$('content').innerHTML='<div class="empty">Erro: '+esc(e.message)+'</div>';}}
async function addProduct(){const value=$('product').value.trim();const groupKey=$('group').value;if(!value)return message('Informe um MLB ou link do Mercado Livre.','err');$('add').disabled=true;message('Salvando...');try{const data=await api('/api/admin/products',{method:'POST',body:JSON.stringify({value,groupKey})});$('product').value='';message(data.inStaticWatchlist?'Produto adicionado. Ele já estava no watchlist.json; não haverá publicação duplicada e o grupo escolhido no Admin prevalece para esta fonte.':'Produto adicionado ao monitoramento permanente.','ok');await load();}catch(e){if(e.status===409&&e.data?.existing)message('Esse produto já está cadastrado no Admin em '+(GROUPS[e.data.existing.group_key]||e.data.existing.group_key)+'. Use Excluir para cadastrá-lo em outro grupo.','err');else message(e.message,'err');}finally{$('add').disabled=false;}}
async function toggleProduct(productId,active){try{await api('/api/admin/products/'+encodeURIComponent(productId),{method:'PATCH',body:JSON.stringify({active})});await load();}catch(e){message(e.message,'err');}}
async function removeProduct(productId){if(!confirm('Remover '+productId+' do monitoramento manual?'))return;try{await api('/api/admin/products/'+encodeURIComponent(productId),{method:'DELETE'});message('Produto removido do Admin.','ok');await load();}catch(e){message(e.message,'err');}}

const statusInfo={pendente:['PENDENTE','pending'],em_analise:['EM ANÁLISE','analysis'],aprovado:['APROVADO',''],rejeitado:['REJEITADO','rejected'],precisa_contato:['PRECISA CONTATO','contact']};
function groupOptions(selected){return Object.entries(GROUPS).map(([k,v])=>'<option value="'+esc(k)+'" '+(k===selected?'selected':'')+'>'+esc(v)+'</option>').join('');}
function digits(v){const d=String(v||'').replace(/\D/g,'');return (d.length===10||d.length===11)?'55'+d:d;}
async function loadRequests(){try{const data=await api('/api/admin/community-requests');if(!data.requests.length){$('requests').innerHTML='<div class="empty">Nenhuma solicitação recebida ainda.</div>';return;}$('requests').innerHTML=data.requests.map(r=>{const si=statusInfo[r.status]||[r.status,'off'];const approvedGroup=r.approved_group_key||r.suggested_group_key;const searchTerm=r.approved_search_term||r.desired_item;const wa=digits(r.whatsapp);return '<div class="request" data-request="'+r.id+'"><div class="request-head"><div><h3>#'+r.id+' — '+esc(r.name)+'</h3><div class="meta">Recebido em '+fmtDate(r.created_at)+' · WhatsApp: <a class="wa" target="_blank" rel="noopener" href="https://wa.me/'+esc(wa)+'">'+esc(r.whatsapp)+'</a></div></div><span class="pill '+si[1]+'">'+si[0]+'</span></div><div class="note"><strong>Pedido:</strong> '+esc(r.desired_item)+'</div><div class="note"><strong>Grupo sugerido:</strong> '+esc(GROUPS[r.suggested_group_key]||r.suggested_group_key)+'</div><div class="note"><strong>Referência:</strong> <a class="ref" target="_blank" rel="noopener" href="'+esc(r.reference_url)+'">'+esc(r.reference_product_id||'Abrir link do Mercado Livre')+'</a></div>'+(r.notes?'<div class="note"><strong>Observação:</strong> '+esc(r.notes)+'</div>':'')+'<div class="request-grid"><div><label>Termo/categoria que será buscado se aprovado</label><input data-field="search" maxlength="180" value="'+esc(searchTerm)+'"></div><div><label>Grupo validado</label><select data-field="group">'+groupOptions(approvedGroup)+'</select></div></div><div class="community-actions"><button class="secondary" data-status="em_analise">Em análise</button><button data-status="aprovado">Aprovar</button><button class="warn" data-status="precisa_contato">Precisa contato</button><button class="danger" data-status="rejeitado">Rejeitar</button></div></div>';}).join('');$('requests').querySelectorAll('[data-request]').forEach(card=>{card.querySelectorAll('[data-status]').forEach(btn=>btn.addEventListener('click',()=>updateRequest(card,btn.dataset.status)));});}catch(e){$('requests').innerHTML='<div class="empty">Erro: '+esc(e.message)+'</div>';}}
async function updateRequest(card,status){const id=card.dataset.request;const search=card.querySelector('[data-field="search"]').value.trim();const groupKey=card.querySelector('[data-field="group"]').value;if(status==='aprovado'&&!search)return alert('Informe o termo/categoria de busca antes de aprovar.');try{await api('/api/admin/community-requests/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify({status,approvedSearchTerm:search,approvedGroupKey:groupKey})});await loadRequests();}catch(e){alert(e.message);}}

$('add').addEventListener('click',addProduct);$('product').addEventListener('keydown',e=>{if(e.key==='Enter')addProduct();});$('refresh').addEventListener('click',load);$('refreshRequests').addEventListener('click',loadRequests);load();loadRequests();
</script>
</body></html>`);
});

app.get("/api/admin/products", adminAuth, async (_req, res) => {
  try {
    await ensureAdminTable();
    const { rows } = await adminPool.query(`
      SELECT a.id, a.product_id, a.group_key, a.active, a.created_at, a.updated_at,
             lastn.price AS last_price,
             lastn.discount_percent AS last_discount,
             lastn.sent_at AS last_sent_at
      FROM admin_monitored_products a
      LEFT JOIN LATERAL (
        SELECT price, discount_percent, sent_at
        FROM product_notifications
        WHERE catalog_product_key = a.product_id
          AND group_key = a.group_key
        ORDER BY sent_at DESC
        LIMIT 1
      ) lastn ON TRUE
      ORDER BY a.created_at DESC
    `);
    res.json({ products: rows.map(row => ({ ...row, in_static_watchlist: isInStaticWatchlist(row.product_id) })) });
  } catch (error) {
    console.error("[admin] list:", error);
    res.status(500).json({ error: "Não foi possível listar os produtos." });
  }
});

app.post("/api/admin/products", adminAuth, async (req, res) => {
  const productId = extractProductId(req.body?.value);
  const groupKey = String(req.body?.groupKey || "").trim();
  if (!productId) return res.status(400).json({ error: "Não encontrei um PRODUCT_ID válido (MLB...) nesse valor." });
  if (!ADMIN_GROUPS[groupKey]) return res.status(400).json({ error: "Grupo inválido." });

  try {
    await ensureAdminTable();
    const existing = await adminPool.query(
      "SELECT id, product_id, group_key, active, created_at, updated_at FROM admin_monitored_products WHERE product_id = $1 LIMIT 1",
      [productId]
    );
    if (existing.rows[0]) {
      return res.status(409).json({ error: "Produto já cadastrado no Admin.", existing: existing.rows[0] });
    }
    const { rows } = await adminPool.query(
      `INSERT INTO admin_monitored_products (product_id, group_key, active, created_at, updated_at)
       VALUES ($1, $2, TRUE, NOW(), NOW())
       RETURNING id, product_id, group_key, active, created_at, updated_at`,
      [productId, groupKey]
    );
    res.status(201).json({ product: rows[0], inStaticWatchlist: isInStaticWatchlist(productId) });
  } catch (error) {
    console.error("[admin] create:", error);
    res.status(500).json({ error: "Não foi possível cadastrar o produto." });
  }
});

app.patch("/api/admin/products/:productId", adminAuth, async (req, res) => {
  const productId = extractProductId(req.params.productId);
  if (!productId) return res.status(400).json({ error: "PRODUCT_ID inválido." });
  const active = req.body?.active;
  const groupKey = req.body?.groupKey;
  if (active === undefined && groupKey === undefined) return res.status(400).json({ error: "Informe active ou groupKey." });
  if (groupKey !== undefined && !ADMIN_GROUPS[String(groupKey)]) return res.status(400).json({ error: "Grupo inválido." });

  try {
    await ensureAdminTable();
    const current = await adminPool.query("SELECT * FROM admin_monitored_products WHERE product_id=$1 LIMIT 1", [productId]);
    if (!current.rows[0]) return res.status(404).json({ error: "Produto não encontrado." });
    const newActive = active === undefined ? current.rows[0].active : Boolean(active);
    const newGroup = groupKey === undefined ? current.rows[0].group_key : String(groupKey);
    const { rows } = await adminPool.query(
      `UPDATE admin_monitored_products SET active=$2, group_key=$3, updated_at=NOW()
       WHERE product_id=$1 RETURNING id, product_id, group_key, active, created_at, updated_at`,
      [productId, newActive, newGroup]
    );
    res.json({ product: rows[0] });
  } catch (error) {
    console.error("[admin] update:", error);
    res.status(500).json({ error: "Não foi possível atualizar o produto." });
  }
});

app.delete("/api/admin/products/:productId", adminAuth, async (req, res) => {
  const productId = extractProductId(req.params.productId);
  if (!productId) return res.status(400).json({ error: "PRODUCT_ID inválido." });
  try {
    await ensureAdminTable();
    const result = await adminPool.query("DELETE FROM admin_monitored_products WHERE product_id=$1", [productId]);
    if (!result.rowCount) return res.status(404).json({ error: "Produto não encontrado." });
    res.json({ ok: true });
  } catch (error) {
    console.error("[admin] delete:", error);
    res.status(500).json({ error: "Não foi possível remover o produto." });
  }
});

app.get("/api/admin/community-requests", adminAuth, async (_req, res) => {
  try {
    await ensureAdminTable();
    const { rows } = await adminPool.query(`
      SELECT id, name, whatsapp, reference_url, reference_product_id,
             desired_item, suggested_group_key, approved_group_key,
             approved_search_term, notes, status, created_at, updated_at
      FROM community_requests
      ORDER BY
        CASE status
          WHEN 'pendente' THEN 1
          WHEN 'em_analise' THEN 2
          WHEN 'precisa_contato' THEN 3
          WHEN 'aprovado' THEN 4
          WHEN 'rejeitado' THEN 5
          ELSE 6
        END,
        created_at DESC
      LIMIT 150
    `);
    res.json({ requests: rows });
  } catch (error) {
    console.error("[admin/community] list:", error);
    res.status(500).json({ error: "Não foi possível listar os pedidos da comunidade." });
  }
});

app.patch("/api/admin/community-requests/:id", adminAuth, async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: "ID inválido." });

  const allowedStatuses = new Set(["pendente", "em_analise", "aprovado", "rejeitado", "precisa_contato"]);
  const status = String(req.body?.status || "").trim();
  const approvedSearchTerm = normalizeText(req.body?.approvedSearchTerm, 180);
  const approvedGroupKey = String(req.body?.approvedGroupKey || "").trim();

  if (!allowedStatuses.has(status)) return res.status(400).json({ error: "Status inválido." });
  if (approvedGroupKey && !ADMIN_GROUPS[approvedGroupKey]) return res.status(400).json({ error: "Grupo validado inválido." });
  if (status === "aprovado" && (!approvedSearchTerm || !ADMIN_GROUPS[approvedGroupKey])) {
    return res.status(400).json({ error: "Para aprovar, informe o termo/categoria de busca e o grupo validado." });
  }

  try {
    await ensureAdminTable();
    const current = await adminPool.query("SELECT * FROM community_requests WHERE id=$1 LIMIT 1", [id]);
    if (!current.rows[0]) return res.status(404).json({ error: "Solicitação não encontrada." });

    const finalSearch = approvedSearchTerm || current.rows[0].approved_search_term || current.rows[0].desired_item;
    const finalGroup = approvedGroupKey || current.rows[0].approved_group_key || current.rows[0].suggested_group_key;

    if (status === "aprovado") {
      const duplicate = await adminPool.query(
        `SELECT id FROM community_requests
         WHERE id <> $1 AND status='aprovado'
           AND LOWER(TRIM(approved_search_term)) = LOWER(TRIM($2))
           AND approved_group_key = $3
         LIMIT 1`,
        [id, finalSearch, finalGroup]
      );
      if (duplicate.rows[0]) {
        return res.status(409).json({ error: `Já existe um pedido aprovado com esse mesmo termo e grupo (#${duplicate.rows[0].id}).` });
      }
    }

    const { rows } = await adminPool.query(
      `UPDATE community_requests
       SET status=$2, approved_search_term=$3, approved_group_key=$4, updated_at=NOW()
       WHERE id=$1
       RETURNING id, name, whatsapp, reference_url, reference_product_id,
                 desired_item, suggested_group_key, approved_group_key,
                 approved_search_term, notes, status, created_at, updated_at`,
      [id, status, finalSearch, finalGroup]
    );
    console.log(`[community] #${id} -> ${status}${status === "aprovado" ? ` | ${finalSearch} -> ${finalGroup}` : ""}`);
    res.json({ request: rows[0] });
  } catch (error) {
    console.error("[admin/community] update:", error);
    res.status(500).json({ error: "Não foi possível atualizar a solicitação." });
  }
});

async function connectWhatsApp() {
  const databaseUrl = process.env.DATABASE_URL?.trim();
  const authProvider = databaseUrl
    ? await useNeonAuthState(databaseUrl)
    : await useMultiFileAuthState("auth_info");
  const { state, saveCreds } = authProvider;

  console.log(
    databaseUrl
      ? "[whatsapp] sessao persistida no PostgreSQL/Neon."
      : "[whatsapp] DATABASE_URL ausente; usando auth_info local."
  );

  sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
    syncFullHistory: false,
    markOnlineOnConnect: false
  });

  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      latestQr = qr;
      console.log("\nQR Code atualizado. Abra /qr?key=SEU_QR_SECRET no navegador.\n");
      // Mantemos o QR no log como fallback.
      qrcodeTerminal.generate(qr, { small: true });
    }

    if (connection === "open") {
      ready = true;
      latestQr = null;
      console.log("WhatsApp conectado.");
    }

    if (connection === "close") {
      ready = false;
      latestQr = null;
      const error = lastDisconnect?.error;
      const statusCode = error instanceof Boom ? error.output?.statusCode : undefined;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(
        "WhatsApp desconectado.",
        shouldReconnect ? "Reconectando..." : "Sessao encerrada."
      );
      if (shouldReconnect) setTimeout(connectWhatsApp, 3000);
    }
  });
}

app.get("/health", (_req, res) =>
  res.json({ ok: true, whatsappReady: ready })
);

app.get("/qr", async (req, res) => {
  const configuredSecret = process.env.QR_SECRET?.trim();
  const suppliedSecret = String(req.query.key || "").trim();

  if (!configuredSecret || suppliedSecret !== configuredSecret) {
    return res.status(401).send("Nao autorizado.");
  }

  if (ready) {
    return res
      .status(200)
      .type("html")
      .send(`<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Auvello WhatsApp</title></head>
<body style="font-family:Arial,sans-serif;text-align:center;padding:40px">
  <h2>WhatsApp conectado ✅</h2>
  <p>A sessao ja esta ativa.</p>
</body>
</html>`);
  }

  if (!latestQr) {
    return res
      .status(200)
      .type("html")
      .send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="3">
  <title>Auvello WhatsApp</title>
</head>
<body style="font-family:Arial,sans-serif;text-align:center;padding:40px">
  <h2>Aguardando QR Code...</h2>
  <p>Esta pagina atualiza automaticamente.</p>
</body>
</html>`);
  }

  try {
    const dataUrl = await QRCode.toDataURL(latestQr, {
      errorCorrectionLevel: "M",
      margin: 2,
      width: 420
    });

    return res
      .status(200)
      .type("html")
      .send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="20">
  <title>Conectar WhatsApp - Auvello</title>
</head>
<body style="font-family:Arial,sans-serif;text-align:center;padding:24px;background:#f7f7f7">
  <div style="max-width:520px;margin:auto;background:white;padding:24px;border-radius:16px">
    <h2>Conectar WhatsApp ao Auvello</h2>
    <p>No celular: WhatsApp → Aparelhos conectados → Conectar aparelho.</p>
    <img src="${dataUrl}" alt="QR Code WhatsApp" style="width:min(420px,100%);height:auto">
    <p style="font-size:13px;color:#666">O QR expira e a pagina atualiza automaticamente.</p>
  </div>
</body>
</html>`);
  } catch (error) {
    console.error("[qr] erro ao gerar QR:", error);
    return res.status(500).send("Nao foi possivel gerar o QR Code.");
  }
});

app.get("/groups", async (_req, res) => {
  if (!ready || !sock) {
    return res.status(503).json({ error: "WhatsApp ainda nao conectado." });
  }
  try {
    const groups = await sock.groupFetchAllParticipating();
    res.json(Object.values(groups).map(g => ({ id: g.id, subject: g.subject })));
  } catch (error) {
    res.status(500).json({ error: String(error) });
  }
});

app.post("/send", async (req, res) => {
  if (!ready || !sock) {
    return res.status(503).json({ error: "WhatsApp ainda nao conectado." });
  }

  const { groupId, message, imageUrl } = req.body || {};

  if (!groupId || !message) {
    return res.status(400).json({ error: "Informe groupId e message." });
  }

  if (!String(groupId).endsWith("@g.us")) {
    return res.status(400).json({ error: "groupId deve terminar com @g.us." });
  }

  try {
    let result;

    if (imageUrl) {
      result = await sock.sendMessage(groupId, {
        image: { url: imageUrl },
        caption: message
      });
    } else {
      result = await sock.sendMessage(groupId, { text: message });
    }

    res.json({ ok: true, id: result?.key?.id || null });
  } catch (error) {
    res.status(500).json({ error: String(error) });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, async () => {
  console.log(`Auvello WhatsApp Service na porta ${PORT}`);
  await connectWhatsApp();
});
