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

app.get("/admin", adminAuth, (_req, res) => {
  res.status(200).type("html").send(`<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Auvello Admin</title>
  <style>
    :root{color-scheme:dark;--bg:#090d0b;--panel:#111815;--panel2:#17201c;--green:#36e676;--text:#f3f7f4;--muted:#8fa298;--danger:#ff6b6b;--border:#26362e}
    *{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at top,#14231b 0,#090d0b 40%);font-family:Inter,Arial,sans-serif;color:var(--text);min-height:100vh}
    .wrap{max-width:1050px;margin:0 auto;padding:28px 18px 70px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:24px}.logo{width:48px;height:48px;border-radius:14px;background:linear-gradient(145deg,#48f98a,#168c48);display:grid;place-items:center;color:#07120b;font-size:25px;font-weight:900;box-shadow:0 0 35px #35e67638}.brand h1{margin:0;font-size:24px}.brand p{margin:4px 0 0;color:var(--muted)}
    .card{background:#111815e8;border:1px solid var(--border);border-radius:18px;padding:20px;box-shadow:0 18px 60px #0005;margin-bottom:18px}h2{font-size:17px;margin:0 0 16px}.form{display:grid;grid-template-columns:1.5fr 1fr auto;gap:10px}input,select,button{height:46px;border-radius:11px;border:1px solid var(--border);font:inherit}input,select{background:#0c120f;color:var(--text);padding:0 13px;outline:none}input:focus,select:focus{border-color:var(--green)}button{padding:0 17px;background:var(--green);color:#06200f;font-weight:800;cursor:pointer;border:none}button.secondary{background:#25332c;color:var(--text)}button.danger{background:#321b1b;color:#ffaaaa;border:1px solid #5e2b2b}.msg{margin-top:12px;min-height:20px;color:var(--muted);font-size:14px}.msg.ok{color:var(--green)}.msg.err{color:#ff8d8d}
    .tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:13px 10px;border-bottom:1px solid var(--border);font-size:14px}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.pill{display:inline-block;padding:5px 9px;border-radius:99px;font-size:12px;font-weight:700;background:#183425;color:#65f49c}.pill.off{background:#332828;color:#c7aaa9}.actions{display:flex;gap:7px}.actions button{height:34px;padding:0 10px;font-size:12px}.empty{color:var(--muted);padding:18px 0}.hint{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.5}.topline{display:flex;align-items:center;justify-content:space-between;gap:12px}.refresh{height:36px!important;background:#25332c!important;color:var(--text)!important}
    @media(max-width:720px){.form{grid-template-columns:1fr}.form button{width:100%}.wrap{padding-top:18px}.brand p{font-size:13px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="brand"><div class="logo">A</div><div><h1>Auvello Admin</h1><p>Monitoramento manual de produtos Mercado Livre</p></div></div>

  <section class="card">
    <h2>Adicionar produto ao monitoramento permanente</h2>
    <div class="form">
      <input id="product" autocomplete="off" placeholder="MLB29089153 ou link completo do Mercado Livre">
      <select id="group">
        ${Object.entries(ADMIN_GROUPS).map(([key,label]) => `<option value="${key}">${label}</option>`).join("")}
      </select>
      <button id="add">+ ADICIONAR</button>
    </div>
    <div class="hint">O cadastro não força publicação. O produto será consultado em todas as rodadas e ainda precisa passar pelas regras atuais de desconto, score, slots e cooldown. “Maiores Descontos” continua automático.</div>
    <div id="message" class="msg"></div>
  </section>

  <section class="card">
    <div class="topline"><h2>Produtos fixados</h2><button class="refresh" id="refresh">Atualizar</button></div>
    <div class="tablewrap"><div id="content" class="empty">Carregando...</div></div>
  </section>
</div>
<script>
const GROUPS = ${JSON.stringify(ADMIN_GROUPS)};
const $ = (id) => document.getElementById(id);
const message = (text, kind='') => { $('message').textContent=text; $('message').className='msg '+kind; };
const fmtDate = (v) => v ? new Date(v).toLocaleString('pt-BR') : '—';
const money = (v) => v == null ? '—' : Number(v).toLocaleString('pt-BR',{style:'currency',currency:'BRL'});

async function api(url, options={}) {
  const res = await fetch(url, {headers:{'Content-Type':'application/json', ...(options.headers||{})}, ...options});
  const data = await res.json().catch(()=>({}));
  if (!res.ok) { const err = new Error(data.error || 'Erro na requisição'); err.status=res.status; err.data=data; throw err; }
  return data;
}

async function load() {
  try {
    const data = await api('/api/admin/products');
    if (!data.products.length) { $('content').innerHTML='<div class="empty">Nenhum produto fixado ainda.</div>'; return; }
    const rows = data.products.map(p =>
      '<tr>'+
      '<td><strong>'+p.product_id+'</strong>'+(p.in_static_watchlist ? '<br><small style="color:#d7b85b">Também está no watchlist.json</small>' : '')+'</td>'+
      '<td>'+(GROUPS[p.group_key] || p.group_key)+'</td>'+
      '<td><span class="pill '+(p.active?'':'off')+'">'+(p.active?'ATIVO':'PAUSADO')+'</span></td>'+
      '<td>'+(p.last_price == null ? '—' : money(p.last_price))+(p.last_discount == null ? '' : '<br><small>'+Number(p.last_discount).toFixed(1)+'% OFF</small>')+'</td>'+
      '<td>'+fmtDate(p.last_sent_at)+'</td>'+
      '<td>'+fmtDate(p.created_at)+'</td>'+
      '<td class="actions">'+
        '<button class="secondary" data-action="toggle" data-product="'+p.product_id+'" data-active="'+(!p.active)+'">'+(p.active?'Pausar':'Ativar')+'</button>'+
        '<button class="danger" data-action="remove" data-product="'+p.product_id+'">Excluir</button>'+
      '</td>'+
      '</tr>'
    ).join('');
    $('content').innerHTML='<table><thead><tr><th>Produto</th><th>Grupo</th><th>Status</th><th>Última publicação</th><th>Publicado em</th><th>Adicionado</th><th>Ações</th></tr></thead><tbody>'+rows+'</tbody></table>';
    $('content').querySelectorAll('[data-action="toggle"]').forEach(btn => btn.addEventListener('click', () => toggleProduct(btn.dataset.product, btn.dataset.active === 'true')));
    $('content').querySelectorAll('[data-action="remove"]').forEach(btn => btn.addEventListener('click', () => removeProduct(btn.dataset.product)));
  } catch (e) { $('content').innerHTML='<div class="empty">Erro: '+String(e.message)+'</div>'; }
}

async function addProduct() {
  const value=$('product').value.trim(); const groupKey=$('group').value;
  if (!value) return message('Informe um MLB ou link do Mercado Livre.','err');
  $('add').disabled=true; message('Salvando...');
  try {
    const data=await api('/api/admin/products',{method:'POST',body:JSON.stringify({value,groupKey})});
    $('product').value='';
    message(data.inStaticWatchlist ? 'Produto adicionado. Ele já estava no watchlist.json; não haverá publicação duplicada e o grupo escolhido no Admin prevalece para esta fonte.' : 'Produto adicionado ao monitoramento permanente.','ok');
    await load();
  } catch(e) {
    if(e.status===409 && e.data?.existing) message('Esse produto já está cadastrado no Admin em '+(GROUPS[e.data.existing.group_key]||e.data.existing.group_key)+'. Use Excluir para cadastrá-lo em outro grupo.','err');
    else message(e.message,'err');
  } finally {$('add').disabled=false;}
}

async function toggleProduct(productId, active) {
  try { await api('/api/admin/products/'+encodeURIComponent(productId),{method:'PATCH',body:JSON.stringify({active})}); await load(); }
  catch(e){ message(e.message,'err'); }
}
async function removeProduct(productId) {
  if(!confirm('Remover '+productId+' do monitoramento manual?')) return;
  try { await api('/api/admin/products/'+encodeURIComponent(productId),{method:'DELETE'}); message('Produto removido do Admin.','ok'); await load(); }
  catch(e){ message(e.message,'err'); }
}
$('add').addEventListener('click',addProduct); $('product').addEventListener('keydown',e=>{if(e.key==='Enter') addProduct();}); $('refresh').addEventListener('click',load); load();
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
