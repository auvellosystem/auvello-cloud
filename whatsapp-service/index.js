import express from "express";
import fs from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
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
const execFileAsync = promisify(execFile);

async function runManualLookup({ productId = null, term = null } = {}) {
  const args = ["manual_lookup.py", "--limit", "3"];
  if (productId) args.push("--product-id", String(productId));
  else if (term) args.push("--term", String(term));
  else throw new Error("Informe productId ou term para a consulta manual.");

  const { stdout, stderr } = await execFileAsync("python3", args, {
    cwd: process.cwd(),
    timeout: 55000,
    maxBuffer: 1024 * 1024,
    env: process.env,
  });
  if (stderr?.trim()) console.log("[manual-lookup]", stderr.trim());
  const text = String(stdout || "").trim();
  if (!text) throw new Error("A consulta não retornou resultado.");
  return JSON.parse(text);
}

const BUILTIN_CATEGORIES = {
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

function builtinGroupIds() {
  return {
    eletronicos_tecnologia: process.env.WA_GROUP_ELETRONICOS?.trim() || null,
    moda_vestuario: process.env.WA_GROUP_MODA?.trim() || null,
    celulares_acessorios: process.env.WA_GROUP_CELULARES?.trim() || null,
    games_acessorios: process.env.WA_GROUP_GAMES?.trim() || null,
    utilidades_domesticas: process.env.WA_GROUP_UTILIDADES?.trim() || null,
    pet_shop: process.env.WA_GROUP_PET?.trim() || null
  };
}

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
  await adminPool.query(`CREATE INDEX IF NOT EXISTS idx_admin_monitored_products_active ON admin_monitored_products(active, group_key)`);

  await adminPool.query(`
    CREATE TABLE IF NOT EXISTS auvello_categories (
      id BIGSERIAL PRIMARY KEY,
      group_key TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      whatsapp_group_id TEXT,
      search_term TEXT,
      active BOOLEAN NOT NULL DEFAULT TRUE,
      public_visible BOOLEAN NOT NULL DEFAULT TRUE,
      mirror_to_general BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
  await adminPool.query(`CREATE INDEX IF NOT EXISTS idx_auvello_categories_active ON auvello_categories(active, public_visible)`);

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
  await adminPool.query(`CREATE INDEX IF NOT EXISTS idx_community_requests_status ON community_requests(status, approved_group_key, created_at)`);
  await adminPool.query(`ALTER TABLE community_requests ADD COLUMN IF NOT EXISTS instant_lookup_count INTEGER NOT NULL DEFAULT 0`);
  await adminPool.query(`ALTER TABLE community_requests ADD COLUMN IF NOT EXISTS instant_response_status TEXT`);
  await adminPool.query(`ALTER TABLE community_requests ADD COLUMN IF NOT EXISTS instant_response_error TEXT`);
  await adminPool.query(`ALTER TABLE community_requests ADD COLUMN IF NOT EXISTS instant_response_at TIMESTAMPTZ`);

  const ids = builtinGroupIds();
  for (const [groupKey, name] of Object.entries(BUILTIN_CATEGORIES)) {
    await adminPool.query(
      `INSERT INTO auvello_categories
       (group_key, name, whatsapp_group_id, active, public_visible, mirror_to_general, created_at, updated_at)
       VALUES ($1,$2,$3,TRUE,TRUE,TRUE,NOW(),NOW())
       ON CONFLICT(group_key) DO NOTHING`,
      [groupKey, name, ids[groupKey]]
    );
  }

  adminTableReady = true;
}

async function listCategories({ activeOnly = false, publicOnly = false } = {}) {
  await ensureAdminTable();
  const conditions = [];
  if (activeOnly) conditions.push("active=TRUE");
  if (publicOnly) conditions.push("public_visible=TRUE");
  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  const { rows } = await adminPool.query(`
    SELECT id, group_key, name, whatsapp_group_id, search_term,
           active, public_visible, mirror_to_general, created_at, updated_at
    FROM auvello_categories
    ${where}
    ORDER BY name ASC
  `);
  return rows;
}

async function getCategoryByKey(groupKey, { activeOnly = false } = {}) {
  await ensureAdminTable();
  const { rows } = await adminPool.query(
    `SELECT id, group_key, name, whatsapp_group_id, search_term,
            active, public_visible, mirror_to_general, created_at, updated_at
     FROM auvello_categories
     WHERE group_key=$1 ${activeOnly ? "AND active=TRUE" : ""}
     LIMIT 1`,
    [groupKey]
  );
  return rows[0] || null;
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
  try { decoded = Buffer.from(auth.slice(6), "base64").toString("utf8"); } catch (_error) { decoded = ""; }
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

  // PRODUCT_IDs do Mercado Livre podem aparecer como MLB... ou MLBU...
  // Em URLs /up/MLBU... o catálogo deve ter prioridade sobre ?item_id=MLB...
  const direct = text.match(/^(MLBU?\d+)$/);
  if (direct) return direct[1];

  const fromProductUrl = text.match(/\/(?:UP|P)\/(MLBU?\d+)/);
  if (fromProductUrl) return fromProductUrl[1];

  const catalogAnywhere = text.match(/\b(MLBU\d{5,})\b/);
  if (catalogAnywhere) return catalogAnywhere[1];

  const anywhere = text.match(/\b(MLB\d{5,})\b/);
  return anywhere ? anywhere[1] : null;
}

function looksLikeUrl(value) {
  const text = String(value || "").trim();
  return /^https?:\/\//i.test(text);
}

function searchTermFromReferenceUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    const parts = url.pathname.split("/").filter(Boolean);
    const stopIndex = parts.findIndex(part => /^(?:up|p)$/i.test(part));
    const slugParts = stopIndex > 0 ? parts.slice(0, stopIndex) : parts.slice(0, 1);
    const slug = slugParts.join(" ")
      .replace(/[-_]+/g, " ")
      .replace(/\bMLBU?\d+\b/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
    return normalizeText(slug, 180);
  } catch (_error) {
    return "";
  }
}

function normalizeText(value, maxLength = 300) {
  return String(value || "").trim().replace(/\s+/g, " ").slice(0, maxLength);
}

function personalWhatsappDigits(value) {
  let digits = String(value || "").replace(/\D/g, "");
  if ((digits.length === 10 || digits.length === 11) && !digits.startsWith("55")) digits = `55${digits}`;
  return digits.slice(0, 15);
}

async function resolvePersonalWhatsappJid(value) {
  if (!ready || !sock) throw new Error("WhatsApp do Auvello ainda não está conectado.");
  const digits = personalWhatsappDigits(value);
  if (digits.length < 12 || digits.length > 15) throw new Error("Número de WhatsApp inválido.");
  try {
    const checks = await sock.onWhatsApp(digits);
    const found = Array.isArray(checks) ? checks.find(item => item?.exists && item?.jid) : null;
    if (found?.jid) return found.jid;
  } catch (error) {
    console.log("[community/private] onWhatsApp:", String(error?.message || error));
  }
  return `${digits}@s.whatsapp.net`;
}

async function sendLookupToCustomer({ whatsapp, name, term, lookup }) {
  const results = Array.isArray(lookup?.results) ? lookup.results.slice(0, 3) : [];
  const sendable = results.filter(result => String(result?.url || "").startsWith("http"));
  const jid = await resolvePersonalWhatsappJid(whatsapp);
  const safeName = normalizeText(name, 80) || "cliente";
  const safeTerm = normalizeText(term, 180) || "produto solicitado";

  if (!results.length) {
    await sock.sendMessage(jid, { text: `Olá, *${safeName}*! 🔎\n\nA Auvello pesquisou *${safeTerm}*, mas não encontrou uma oferta relevante agora.\n\nSeu pedido foi registrado e ficará pendente para análise.` });
    return { sent: 0, status: "sem_resultado", jid };
  }
  if (!sendable.length) {
    await sock.sendMessage(jid, { text: `Olá, *${safeName}*! 🔎\n\nA Auvello encontrou resultados para *${safeTerm}*, mas não conseguiu gerar links de afiliado seguros neste momento.\n\nSeu pedido foi registrado e ficará pendente para análise.` });
    return { sent: 0, status: "sem_link_afiliado", jid };
  }

  await sock.sendMessage(jid, { text: `Olá, *${safeName}*! 🔎\n\nA Auvello encontrou ${sendable.length} oferta(s) para *${safeTerm}*:` });
  let sent = 0;
  for (const result of sendable) {
    const message = lookupDispatchMessage(result);
    const picture = String(result?.picture || "").trim();
    if (picture) await sock.sendMessage(jid, { image: { url: picture }, caption: message });
    else await sock.sendMessage(jid, { text: message });
    sent += 1;
  }
  await sock.sendMessage(jid, { text: "✅ Seu pedido também foi registrado no Auvello e ficará pendente para análise." });
  return { sent, status: "enviado", jid };
}

function normalizeWhatsapp(value) {
  return String(value || "").replace(/[^0-9+()\-\s]/g, "").trim().slice(0, 30);
}

function validateMercadoLivreUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    const validHost = host === "mercadolivre.com.br" || host.endsWith(".mercadolivre.com.br") || host === "mercadolibre.com" || host.endsWith(".mercadolibre.com");
    if (!validHost || !["http:", "https:"].includes(url.protocol)) return null;
    return url.toString().slice(0, 2000);
  } catch (_error) { return null; }
}

function normalizeGroupId(value) {
  const v = String(value || "").trim();
  if (!v) return null;
  return v.endsWith("@g.us") ? v.slice(0, 180) : null;
}

function slugifyGroupKey(value) {
  return normalizeText(value, 100)
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
    .slice(0, 70);
}

function communityRateAllowed(req) {
  const now = Date.now();
  const windowMs = 60 * 60 * 1000;
  const limit = 5;
  const key = String(req.headers["x-forwarded-for"] || req.socket.remoteAddress || "unknown").split(",")[0].trim();
  const recent = (communityRate.get(key) || []).filter(ts => now - ts < windowMs);
  if (recent.length >= limit) return false;
  recent.push(now); communityRate.set(key, recent); return true;
}

function isInStaticWatchlist(productId) {
  try {
    const raw = fs.readFileSync("watchlist.json", "utf8");
    const data = JSON.parse(raw);
    const ids = Array.isArray(data.product_identifiers) ? data.product_identifiers : [];
    return ids.some((id) => String(id).trim().toUpperCase() === productId);
  } catch (_error) { return false; }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

app.get("/pedir-oferta", async (_req, res) => {
  let categories = [];
  try { categories = await listCategories({ activeOnly: true, publicOnly: true }); }
  catch (error) { console.error("[community] categorias públicas:", error); }
  const options = categories.map(c => `<option value="${esc(c.group_key)}">${esc(c.name)}</option>`).join("");
  res.status(200).type("html").send(`<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pedir uma oferta - Auvello</title>
<style>
:root{color-scheme:dark;--bg:#090d0b;--panel:#111815;--green:#36e676;--text:#f3f7f4;--muted:#93a69b;--border:#26362e}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at top,#183023 0,#090d0b 42%);font-family:Inter,Arial,sans-serif;color:var(--text)}.wrap{max-width:760px;margin:0 auto;padding:30px 18px 70px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:24px}.logo{width:52px;height:52px;border-radius:15px;background:linear-gradient(145deg,#48f98a,#168c48);display:grid;place-items:center;color:#07120b;font-size:27px;font-weight:900}.brand h1{margin:0;font-size:25px}.brand p{margin:4px 0 0;color:var(--muted)}.card{background:#111815ee;border:1px solid var(--border);border-radius:20px;padding:22px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;font-size:13px;font-weight:750;margin-bottom:7px}.help{font-size:12px;color:var(--muted);line-height:1.45;margin-top:6px}input,select,textarea,button{width:100%;border-radius:11px;border:1px solid var(--border);font:inherit}input,select,textarea{background:#0c120f;color:var(--text);padding:12px 13px;outline:none}input,select{height:46px}textarea{min-height:92px;resize:vertical}button{height:50px;background:var(--green);color:#06200f;font-weight:900;cursor:pointer;border:none}.notice{padding:14px 15px;border-radius:13px;background:#171b13;border:1px solid #4b4524;color:#e8e2c8;font-size:13px;line-height:1.55}.msg{min-height:22px;margin-top:13px;font-size:14px;color:var(--muted)}.msg.ok{color:var(--green)}.msg.err{color:#ff9090}.honeypot{position:absolute;left:-10000px;opacity:0;pointer-events:none}@media(max-width:650px){.grid{grid-template-columns:1fr}.full{grid-column:auto}}
</style></head><body><div class="wrap">
<div class="brand"><div class="logo">A</div><div><h1>Encontre uma oferta com a Auvello</h1><p>Peça um produto e receba os resultados no seu WhatsApp.</p></div></div>
<div class="card"><div class="grid">
<div><label for="name">Nome</label><input id="name" maxlength="80" autocomplete="name" placeholder="Seu nome"></div>
<div><label for="whatsapp">WhatsApp para receber as ofertas</label><input id="whatsapp" maxlength="30" inputmode="tel" autocomplete="tel" placeholder="(27) 99999-9999"><div class="help"><strong>O Auvello pesquisará agora e enviará os resultados desta solicitação para este número. Seu pedido também ficará pendente para análise.</strong></div></div>
<div class="full notice"><strong>Informe pelo menos uma das duas opções abaixo:</strong> descreva o que procura <strong>ou</strong> cole um link do Mercado Livre. Você também pode preencher os dois.</div>
<div class="full"><label for="desiredItem">O que você gostaria que o Auvello buscasse? (opcional se enviar link)</label><input id="desiredItem" maxlength="180" placeholder="Ex.: SSD NVMe 1TB, ração para gatos adultos, Air Fryer 5L"></div>
<div class="full"><label for="referenceUrl">Link de referência do Mercado Livre (opcional se descrever)</label><input id="referenceUrl" maxlength="2000" inputmode="url" placeholder="https://www.mercadolivre.com.br/..."><div class="help">Se você só tiver o link, pode enviar assim. O Auvello poderá definir manualmente o termo de busca durante a análise.</div></div>
<div><label for="groupKey">Categoria / grupo sugerido</label><select id="groupKey">${options}</select><div class="help">É apenas uma sugestão e pode ser ajustada na análise.</div></div>
<div><label for="notes">Observação (opcional)</label><textarea id="notes" maxlength="500" placeholder="Ex.: prefiro pacote de 10 kg ou mais"></textarea></div>
<div class="honeypot"><input id="company" tabindex="-1" autocomplete="off"></div>
<div class="full notice"><strong>Importante:</strong> o link é referência. A solicitação não garante o monitoramento daquele anúncio específico. Se aprovada, o Auvello transforma o pedido em um termo de busca associado à categoria correta.</div>
<div class="full"><button id="send">BUSCAR MINHA OFERTA</button><div id="message" class="msg"></div></div>
</div></div></div>
<script>
const $=id=>document.getElementById(id);const msg=(t,k='')=>{$('message').textContent=t;$('message').className='msg '+k;};
$('send').addEventListener('click',async()=>{const payload={name:$('name').value.trim(),whatsapp:$('whatsapp').value.trim(),referenceUrl:$('referenceUrl').value.trim(),desiredItem:$('desiredItem').value.trim(),groupKey:$('groupKey').value,notes:$('notes').value.trim(),company:$('company').value};if(!payload.name||!payload.whatsapp)return msg('Preencha nome e WhatsApp.','err');if(!payload.referenceUrl&&!payload.desiredItem)return msg('Informe o que deseja encontrar ou um link do Mercado Livre.','err');if(!payload.groupKey)return msg('Selecione uma categoria.','err');$('send').disabled=true;msg('Pesquisando ofertas e preparando o envio para seu WhatsApp...');try{const r=await fetch('/api/community/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||'Não foi possível enviar.');if(d.instant?.status==='enviado')msg('Pronto! '+(d.instant.sent||0)+' oferta(s) enviada(s) para seu WhatsApp. Seu pedido também ficou pendente para análise.','ok');else if(d.instant?.status==='sem_resultado')msg('Pedido registrado. Não encontramos uma oferta relevante agora; ele ficou pendente para análise.','ok');else if(d.instant?.status==='sem_link_afiliado')msg('Pedido registrado. Encontramos resultados, mas os links não puderam ser preparados agora. O pedido ficou pendente para análise.','ok');else msg('Pedido registrado para análise. Não foi possível entregar a resposta automática no WhatsApp agora.','ok');$('referenceUrl').value='';$('desiredItem').value='';$('notes').value='';}catch(e){msg(e.message,'err');}finally{$('send').disabled=false;}});
</script></body></html>`);
});

app.post("/api/community/requests", async (req, res) => {
  if (!communityRateAllowed(req)) return res.status(429).json({ error: "Muitas solicitações em pouco tempo. Tente novamente mais tarde." });
  if (req.body?.company) return res.status(201).json({ ok: true });
  const name = normalizeText(req.body?.name, 80);
  const whatsapp = normalizeWhatsapp(req.body?.whatsapp);
  const rawReference = String(req.body?.referenceUrl || "").trim();
  const referenceUrl = rawReference ? validateMercadoLivreUrl(rawReference) : null;
  const desiredItem = normalizeText(req.body?.desiredItem, 180);
  const suggestedGroupKey = String(req.body?.groupKey || "").trim();
  const notes = normalizeText(req.body?.notes, 500) || null;
  if (name.length < 2) return res.status(400).json({ error: "Informe seu nome." });
  if (whatsapp.replace(/\D/g, "").length < 10) return res.status(400).json({ error: "Informe um WhatsApp válido para receber as ofertas." });
  if (!desiredItem && !rawReference) return res.status(400).json({ error: "Informe o que deseja encontrar ou um link do Mercado Livre." });
  if (rawReference && !referenceUrl) return res.status(400).json({ error: "O link informado não é um link válido do Mercado Livre." });
  const category = await getCategoryByKey(suggestedGroupKey, { activeOnly: true }).catch(() => null);
  if (!category || !category.public_visible) return res.status(400).json({ error: "Categoria sugerida inválida." });

  try {
    await ensureAdminTable();
    const referenceProductId = referenceUrl ? extractProductId(referenceUrl) : null;
    const urlSearchTerm = searchTermFromReferenceUrl(referenceUrl);
    let instantSearchTerm = desiredItem || urlSearchTerm || "";
    if (looksLikeUrl(instantSearchTerm)) instantSearchTerm = urlSearchTerm;

    const { rows } = await adminPool.query(
      `INSERT INTO community_requests
       (name, whatsapp, reference_url, reference_product_id, desired_item, suggested_group_key, notes, status, created_at, updated_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,'pendente',NOW(),NOW()) RETURNING id,status,created_at`,
      [name, whatsapp, referenceUrl || "", referenceProductId, desiredItem || "", suggestedGroupKey, notes]
    );
    const requestRow = rows[0];
    console.log(`[community] nova solicitação #${requestRow.id}: ${desiredItem || "[somente link]"} -> ${suggestedGroupKey}`);

    let lookup = null;
    let instant = { status: "erro_consulta", sent: 0, error: null };
    try {
      if (referenceProductId && /^MLB\d+$/i.test(referenceProductId) && !/^MLBU/i.test(referenceProductId)) {
        lookup = await runManualLookup({ productId: referenceProductId });
      }
      if (!lookup?.results?.length && instantSearchTerm) {
        lookup = await runManualLookup({ term: instantSearchTerm });
      }

      const delivery = await sendLookupToCustomer({ whatsapp, name, term: instantSearchTerm || desiredItem || "produto solicitado", lookup });
      instant = { status: delivery.status, sent: delivery.sent, error: null };
      await adminPool.query(
        `UPDATE community_requests SET instant_lookup_count=$2, instant_response_status=$3, instant_response_error=NULL, instant_response_at=NOW(), updated_at=NOW() WHERE id=$1`,
        [requestRow.id, Number(lookup?.results?.length || 0), delivery.status]
      );
      console.log(`[community/private] #${requestRow.id}: ${delivery.status} | ${delivery.sent} enviada(s)`);
    } catch (error) {
      const message = String(error?.message || error).slice(0, 800);
      instant = { status: "erro_whatsapp", sent: 0, error: message };
      await adminPool.query(
        `UPDATE community_requests SET instant_lookup_count=$2, instant_response_status=$3, instant_response_error=$4, instant_response_at=NOW(), updated_at=NOW() WHERE id=$1`,
        [requestRow.id, Number(lookup?.results?.length || 0), instant.status, message]
      ).catch(() => null);
      console.error(`[community/private] #${requestRow.id}:`, error);
    }

    res.status(201).json({ ok: true, request: requestRow, instant });
  } catch (error) {
    console.error("[community] create:", error);
    res.status(500).json({ error: "Não foi possível registrar sua solicitação." });
  }
});

app.get("/admin", adminAuth, (_req, res) => {
  res.status(200).type("html").send(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Auvello Admin</title>
<style>
:root{color-scheme:dark;--bg:#090d0b;--panel:#111815;--green:#36e676;--text:#f3f7f4;--muted:#8fa298;--border:#26362e}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#14231b 0,#090d0b 40%);font-family:Inter,Arial,sans-serif;color:var(--text);min-height:100vh}.wrap{max-width:1180px;margin:0 auto;padding:28px 18px 70px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:24px}.logo{width:48px;height:48px;border-radius:14px;background:linear-gradient(145deg,#48f98a,#168c48);display:grid;place-items:center;color:#07120b;font-size:25px;font-weight:900}.brand h1{margin:0;font-size:24px}.brand p{margin:4px 0 0;color:var(--muted)}.card{background:#111815e8;border:1px solid var(--border);border-radius:18px;padding:20px;margin-bottom:18px}h2{font-size:17px;margin:0 0 16px}.form{display:grid;grid-template-columns:minmax(190px,1.05fr) minmax(250px,1.15fr) minmax(360px,2fr) max-content;gap:10px;align-items:start}.form3{display:grid;grid-template-columns:1.5fr 1fr auto;gap:10px}input,select,textarea,button{border-radius:11px;border:1px solid var(--border);font:inherit}input,select,textarea{background:#0c120f;color:var(--text);padding:0 13px;outline:none}input,select{height:46px}textarea{padding:10px 13px;min-height:70px}button{height:46px;padding:0 17px;background:var(--green);color:#06200f;font-weight:800;cursor:pointer;border:none}#catAdd{min-width:118px;white-space:nowrap;align-self:start}button.secondary{background:#25332c;color:var(--text)}button.danger{background:#321b1b;color:#ffaaaa;border:1px solid #5e2b2b}.checks{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}.checks label{font-size:13px;color:#cbd6cf}.checks input{height:auto;width:auto}.msg{margin-top:12px;min-height:20px;color:var(--muted);font-size:14px}.msg.ok{color:var(--green)}.msg.err{color:#ff8d8d}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:850px}th,td{text-align:left;padding:12px 9px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase}.pill{display:inline-block;padding:5px 9px;border-radius:99px;font-size:12px;font-weight:700;background:#183425;color:#65f49c}.pill.off{background:#332828;color:#c7aaa9}.actions{display:flex;gap:7px;flex-wrap:wrap}.actions button{height:34px;padding:0 10px;font-size:12px}.empty{color:var(--muted);padding:18px 0}.hint{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.5}.topline{display:flex;align-items:center;justify-content:space-between;gap:12px}.refresh{height:36px!important;background:#25332c!important;color:var(--text)!important}.request{border:1px solid var(--border);border-radius:15px;padding:15px;margin:11px 0;background:#0d1410}.request-head{display:flex;justify-content:space-between;gap:12px}.request h3{margin:0 0 5px;font-size:16px}.meta,.note{color:#aab9b0;font-size:12px;line-height:1.5}.note{margin-top:9px;font-size:13px}.request-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:13px}.request-grid label{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}.request-grid input,.request-grid select{width:100%;height:40px}.community-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.community-actions button{height:36px;font-size:12px}.cat-edit{display:grid;grid-template-columns:1fr 1fr;gap:6px}.cat-edit input,.cat-edit select{height:36px;width:100%}.lookup-backdrop{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;padding:18px;z-index:1000}.lookup-backdrop.open{display:flex}.lookup-modal{width:min(760px,100%);max-height:88vh;overflow:auto;background:#101713;border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 24px 80px #0009}.lookup-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.lookup-head h3{margin:0}.lookup-close{height:36px!important;background:#25332c!important;color:var(--text)!important}.lookup-results{display:grid;gap:10px}.lookup-item{border:1px solid var(--border);border-radius:13px;padding:13px;background:#0b120e}.lookup-item strong{display:block;margin-bottom:6px}.lookup-price{font-size:18px;font-weight:900}.lookup-meta{font-size:12px;color:var(--muted);margin-top:5px}.lookup-item a{display:inline-block;margin-top:9px;color:#72eda0;font-weight:800;text-decoration:none}.lookup-dispatch{margin-top:16px;padding-top:14px;border-top:1px solid var(--border);display:grid;grid-template-columns:minmax(220px,1fr) auto;gap:9px;align-items:end}.lookup-dispatch label{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}.lookup-dispatch select{width:100%}.lookup-dispatch button{min-width:150px}.lookup-dispatch-status{grid-column:1/-1;min-height:18px;font-size:13px;color:var(--muted)}.lookup-dispatch-status.ok{color:var(--green)}.lookup-dispatch-status.err{color:#ff8d8d}@media(max-width:980px){.form{grid-template-columns:1fr 1fr}.form #catWhatsApp{grid-column:1/-1}.form #catAdd{grid-column:1/-1;width:100%}.form3{grid-template-columns:1fr}.request-grid{grid-template-columns:1fr}.cat-edit{grid-template-columns:1fr}}@media(max-width:650px){.form{grid-template-columns:1fr}.form #catWhatsApp,.form #catAdd{grid-column:auto}.lookup-dispatch{grid-template-columns:1fr}.lookup-dispatch button{width:100%}}
</style></head><body><div class="wrap"><div class="brand"><div class="logo">A</div><div><h1>Auvello Admin</h1><p>Categorias, grupos, produtos fixados e pedidos da comunidade</p></div></div>
<section class="card"><h2>Categorias e grupos</h2><div class="form"><input id="catName" placeholder="Nome da nova categoria"><textarea id="catSearch" placeholder="Buscas automáticas (opcional) — uma por linha ou separadas por vírgula"></textarea><select id="catWhatsApp"><option value="">Sem grupo por enquanto</option></select><button id="catAdd">+ CRIAR</button></div><div class="checks"><label><input type="checkbox" id="catPublic" checked> Exibir no formulário público</label><label><input type="checkbox" id="catMirror" checked> Espelhar ofertas no Geral</label></div><div class="hint">Crie o grupo no WhatsApp, depois selecione-o aqui. Em “Buscas automáticas”, você pode cadastrar até 15 termos, um por linha ou separados por vírgula/ponto e vírgula. Cada termo será pesquisado separadamente e os resultados serão reunidos nesta categoria. Sem termos, a categoria ainda pode receber produtos fixados e pedidos aprovados.</div><div id="catMessage" class="msg"></div><div class="tablewrap"><div id="categories" class="empty">Carregando...</div></div></section>
<section class="card"><h2>Adicionar produto ao monitoramento permanente</h2><div class="form3"><input id="product" placeholder="MLB29089153 ou link completo do Mercado Livre"><select id="group"></select><button id="add">+ ADICIONAR</button></div><div class="hint">O produto continua precisando passar pelos critérios atuais. O grupo Geral não é selecionável: ofertas específicas podem ser espelhadas para ele automaticamente.</div><div id="message" class="msg"></div></section>
<section class="card"><div class="topline"><h2>Produtos fixados</h2><button class="refresh" id="refresh">Atualizar</button></div><div class="tablewrap"><div id="content" class="empty">Carregando...</div></div></section>
<section class="card"><div class="topline"><div><h2>Pedidos da Comunidade</h2><div class="hint" style="margin-top:-8px">O cliente recebe uma consulta automática no WhatsApp ao enviar o pedido. A solicitação continua pendente aqui; ao aprovar, o termo entra nas buscas automáticas.</div></div><button class="refresh" id="refreshRequests">Atualizar</button></div><div id="requests" class="empty">Carregando...</div></section>
</div><div id="lookupBackdrop" class="lookup-backdrop"><div class="lookup-modal"><div class="lookup-head"><h3 id="lookupTitle">Consulta</h3><button id="lookupClose" class="lookup-close">Fechar</button></div><div id="lookupBody" class="lookup-results"></div><div id="lookupDispatch" class="lookup-dispatch" style="display:none"><div><label for="lookupGroup">Enviar ofertas para</label><select id="lookupGroup"></select></div><button id="lookupSend">DISPARAR OFERTAS</button><div id="lookupDispatchStatus" class="lookup-dispatch-status"></div></div></div></div><script>
const $=id=>document.getElementById(id);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmtDate=v=>v?new Date(v).toLocaleString('pt-BR'):'—';const money=v=>v==null?'—':Number(v).toLocaleString('pt-BR',{style:'currency',currency:'BRL'});async function api(url,options={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const d=await r.json().catch(()=>({}));if(!r.ok){const e=new Error(d.error||'Erro na requisição');e.status=r.status;e.data=d;throw e;}return d;}
let CURRENT_LOOKUP_RESULTS=[];
function preferredGeneralGroupId(){const exact=WA_GROUPS.find(g=>/^auvello\s*-?\s*geral$/i.test(String(g.subject||'').trim()));if(exact)return exact.id;const geral=WA_GROUPS.find(g=>/\bgeral\b/i.test(String(g.subject||'')));return geral?.id||'';}
function lookupGroupOptions(){const preferred=preferredGeneralGroupId();return WA_GROUPS.map(g=>'<option value="'+esc(g.id)+'" '+(g.id===preferred?'selected':'')+'>'+esc(g.subject)+' — '+esc(g.id)+'</option>').join('');}
function showLookup(title,lookup,errorText=''){const results=lookup?.results||[];CURRENT_LOOKUP_RESULTS=results;$('lookupTitle').textContent=title;let html='';if(errorText)html='<div class="empty">Erro na consulta: '+esc(errorText)+'</div>';else if(!results.length)html='<div class="empty">Nenhuma oferta encontrada nesta consulta.</div>';else html=results.map((r,i)=>'<div class="lookup-item"><strong>'+(i+1)+'. '+esc(r.name||r.product_id)+'</strong><div class="lookup-price">'+money(r.price)+(r.discount_percent>0?' · '+Number(r.discount_percent).toFixed(1)+'% OFF':'')+'</div><div class="lookup-meta">PRODUCT: '+esc(r.product_id)+(r.item_id?' · ITEM: '+esc(r.item_id):'')+(r.search_rank?' · relevância #'+esc(r.search_rank):'')+'</div>'+(r.url?'<a href="'+esc(r.url)+'" target="_blank" rel="noopener">Abrir com link Auvello</a>':'<div class="lookup-meta">Link de afiliado indisponível nesta consulta.</div>')+'</div>').join('');$('lookupBody').innerHTML=html;const sendable=results.length>0&&results.every(r=>r.url);$('lookupDispatch').style.display=results.length?'grid':'none';$('lookupGroup').innerHTML=lookupGroupOptions();$('lookupSend').disabled=!sendable||!WA_GROUPS.length;$('lookupDispatchStatus').className='lookup-dispatch-status';$('lookupDispatchStatus').textContent=!WA_GROUPS.length&&results.length?'WhatsApp sem grupos disponíveis agora.':(!sendable&&results.length?'Uma ou mais ofertas estão sem link de afiliado e não podem ser disparadas.':'');$('lookupBackdrop').classList.add('open');}
$('lookupSend').onclick=async()=>{const groupId=$('lookupGroup').value;if(!groupId||!CURRENT_LOOKUP_RESULTS.length)return;const button=$('lookupSend');button.disabled=true;const original=button.textContent;button.textContent='ENVIANDO...';$('lookupDispatchStatus').className='lookup-dispatch-status';$('lookupDispatchStatus').textContent='Enviando '+CURRENT_LOOKUP_RESULTS.length+' oferta(s)...';try{const d=await api('/api/admin/lookup-dispatch',{method:'POST',body:JSON.stringify({groupId,results:CURRENT_LOOKUP_RESULTS})});$('lookupDispatchStatus').className='lookup-dispatch-status ok';$('lookupDispatchStatus').textContent=(d.sent||0)+' oferta(s) enviada(s) com sucesso.';}catch(e){$('lookupDispatchStatus').className='lookup-dispatch-status err';$('lookupDispatchStatus').textContent=e.message;}finally{button.disabled=false;button.textContent=original;}};
$('lookupClose').onclick=()=>$('lookupBackdrop').classList.remove('open');$('lookupBackdrop').onclick=e=>{if(e.target===$('lookupBackdrop'))$('lookupBackdrop').classList.remove('open');};
let CATEGORIES=[];let WA_GROUPS=[];const catMap=()=>Object.fromEntries(CATEGORIES.map(c=>[c.group_key,c.name]));function activeOptions(selected=''){return CATEGORIES.filter(c=>c.active).map(c=>'<option value="'+esc(c.group_key)+'" '+(c.group_key===selected?'selected':'')+'>'+esc(c.name)+'</option>').join('');}function waOptions(selected=''){const known=WA_GROUPS.some(g=>g.id===selected);const extra=selected&&!known?'<option value="'+esc(selected)+'" selected>Grupo atual — '+esc(selected)+'</option>':'';return '<option value="">Sem grupo</option>'+extra+WA_GROUPS.map(g=>'<option value="'+esc(g.id)+'" '+(g.id===selected?'selected':'')+'>'+esc(g.subject)+' — '+esc(g.id)+'</option>').join('');}
async function loadWhatsAppGroups(){try{const d=await api('/api/admin/whatsapp-groups');WA_GROUPS=d.groups||[];}catch(_e){WA_GROUPS=[];}}
async function loadCategories(){const d=await api('/api/admin/categories');CATEGORIES=d.categories||[];$('group').innerHTML=activeOptions();$('catWhatsApp').innerHTML=waOptions();if(!CATEGORIES.length){$('categories').innerHTML='<div class="empty">Nenhuma categoria.</div>';return;}$('categories').innerHTML='<table><thead><tr><th>Categoria</th><th>Buscas automáticas</th><th>Grupo WhatsApp</th><th>Opções</th><th>Ações</th></tr></thead><tbody>'+CATEGORIES.map(c=>'<tr data-cat="'+c.id+'"><td><strong>'+esc(c.name)+'</strong><br><small>'+esc(c.group_key)+'</small></td><td><textarea data-f="search" placeholder="Uma busca por linha">'+esc(c.search_term||'')+'</textarea></td><td><select data-f="wa">'+waOptions(c.whatsapp_group_id||'')+'</select></td><td><label><input data-f="active" type="checkbox" '+(c.active?'checked':'')+'> ativo</label><br><label><input data-f="public" type="checkbox" '+(c.public_visible?'checked':'')+'> público</label><br><label><input data-f="mirror" type="checkbox" '+(c.mirror_to_general?'checked':'')+'> → Geral</label></td><td class="actions"><button data-action="save">Salvar</button><button class="danger" data-action="delete">Excluir</button></td></tr>').join('')+'</tbody></table>';$('categories').querySelectorAll('[data-cat]').forEach(row=>{row.querySelector('[data-action="save"]').onclick=()=>saveCategory(row);row.querySelector('[data-action="delete"]').onclick=()=>deleteCategory(row);});}
async function createCategory(){const name=$('catName').value.trim(),searchTerm=$('catSearch').value.trim(),whatsappGroupId=$('catWhatsApp').value;if(!name)return $('catMessage').textContent='Informe o nome da categoria.';try{await api('/api/admin/categories',{method:'POST',body:JSON.stringify({name,searchTerm,whatsappGroupId,publicVisible:$('catPublic').checked,mirrorToGeneral:$('catMirror').checked})});$('catName').value='';$('catSearch').value='';$('catMessage').textContent='Categoria criada.';await loadCategories();await loadProducts();await loadRequests();}catch(e){$('catMessage').textContent=e.message;}}
async function saveCategory(row){const id=row.dataset.cat;try{await api('/api/admin/categories/'+id,{method:'PATCH',body:JSON.stringify({searchTerm:row.querySelector('[data-f="search"]').value.trim(),whatsappGroupId:row.querySelector('[data-f="wa"]').value,active:row.querySelector('[data-f="active"]').checked,publicVisible:row.querySelector('[data-f="public"]').checked,mirrorToGeneral:row.querySelector('[data-f="mirror"]').checked})});$('catMessage').textContent='Categoria atualizada.';await loadCategories();}catch(e){alert(e.message);}}
async function deleteCategory(row){if(!confirm('Excluir esta categoria? Se ela já estiver em uso, o sistema impedirá a exclusão.'))return;try{await api('/api/admin/categories/'+row.dataset.cat,{method:'DELETE'});await loadCategories();}catch(e){alert(e.message);}}
async function loadProducts(){try{const d=await api('/api/admin/products');const M=catMap();if(!d.products.length){$('content').innerHTML='<div class="empty">Nenhum produto fixado ainda.</div>';return;}$('content').innerHTML='<table><thead><tr><th>Produto</th><th>Categoria</th><th>Status</th><th>Última publicação</th><th>Publicado em</th><th>Adicionado</th><th>Ações</th></tr></thead><tbody>'+d.products.map(p=>'<tr><td><strong>'+esc(p.product_id)+'</strong></td><td>'+esc(M[p.group_key]||p.group_key)+'</td><td><span class="pill '+(p.active?'':'off')+'">'+(p.active?'ATIVO':'PAUSADO')+'</span></td><td>'+(p.last_price==null?'—':money(p.last_price))+(p.last_discount==null?'':'<br><small>'+Number(p.last_discount).toFixed(1)+'% OFF</small>')+'</td><td>'+fmtDate(p.last_sent_at)+'</td><td>'+fmtDate(p.created_at)+'</td><td class="actions"><button data-lookup="'+esc(p.product_id)+'">Consultar agora</button><button class="secondary" data-toggle="'+esc(p.product_id)+'" data-active="'+(!p.active)+'">'+(p.active?'Pausar':'Ativar')+'</button><button class="danger" data-remove="'+esc(p.product_id)+'">Excluir</button></td></tr>').join('')+'</tbody></table>';$('content').querySelectorAll('[data-lookup]').forEach(b=>b.onclick=async()=>{const original=b.textContent;b.disabled=true;b.textContent='Consultando...';try{const d=await api('/api/admin/products/'+encodeURIComponent(b.dataset.lookup)+'/lookup',{method:'POST',body:'{}'});showLookup('Consulta manual — '+b.dataset.lookup,d.lookup,d.lookup_error);}catch(e){showLookup('Consulta manual — '+b.dataset.lookup,null,e.message);}finally{b.disabled=false;b.textContent=original;}});$('content').querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{await api('/api/admin/products/'+encodeURIComponent(b.dataset.toggle),{method:'PATCH',body:JSON.stringify({active:b.dataset.active==='true'})});loadProducts();});$('content').querySelectorAll('[data-remove]').forEach(b=>b.onclick=async()=>{if(confirm('Remover produto?')){await api('/api/admin/products/'+encodeURIComponent(b.dataset.remove),{method:'DELETE'});loadProducts();}});}catch(e){$('content').innerHTML='<div class="empty">Erro: '+esc(e.message)+'</div>';}}
async function addProduct(){const value=$('product').value.trim(),groupKey=$('group').value;if(!value||!groupKey)return;try{await api('/api/admin/products',{method:'POST',body:JSON.stringify({value,groupKey})});$('product').value='';$('message').textContent='Produto adicionado.';loadProducts();}catch(e){$('message').textContent=e.message;}}
const statusInfo={pendente:'PENDENTE',em_analise:'EM ANÁLISE',aprovado:'APROVADO',rejeitado:'REJEITADO',precisa_contato:'PRECISA CONTATO'};function digits(v){const d=String(v||'').replace(/\D/g,'');return(d.length===10||d.length===11)?'55'+d:d;}
async function loadRequests(){try{const d=await api('/api/admin/community-requests');const M=catMap();if(!d.requests.length){$('requests').innerHTML='<div class="empty">Nenhuma solicitação.</div>';return;}$('requests').innerHTML=d.requests.map(r=>{const group=r.approved_group_key||r.suggested_group_key;const search=r.approved_search_term||r.desired_item||'';const ref=r.reference_url?'<div class="note"><strong>Link:</strong> <a target="_blank" style="color:#72eda0" href="'+esc(r.reference_url)+'">abrir referência</a></div>':'';const pedido=r.desired_item?'<div class="note"><strong>Pedido:</strong> '+esc(r.desired_item)+'</div>':'<div class="note"><strong>Pedido:</strong> somente link de referência</div>';const instantLabel=r.instant_response_status==='enviado'?'✅ Resposta automática: '+Number(r.instant_lookup_count||0)+' resultado(s), enviada em '+fmtDate(r.instant_response_at):r.instant_response_status==='sem_resultado'?'🔎 Resposta automática: nenhuma oferta relevante encontrada em '+fmtDate(r.instant_response_at):r.instant_response_status==='sem_link_afiliado'?'⚠️ Resposta automática: resultados encontrados, mas sem link afiliado disponível':r.instant_response_status?'⚠️ Resposta automática falhou'+(r.instant_response_error?' — '+esc(r.instant_response_error):''):'⏳ Resposta automática ainda não registrada';const instantNote='<div class="note"><strong>'+instantLabel+'</strong></div>';return '<div class="request" data-request="'+r.id+'"><div class="request-head"><div><h3>#'+r.id+' — '+esc(r.name)+'</h3><div class="meta">'+fmtDate(r.created_at)+' · <a style="color:#72eda0" target="_blank" href="https://wa.me/'+esc(digits(r.whatsapp))+'">'+esc(r.whatsapp)+'</a></div></div><span class="pill">'+esc(statusInfo[r.status]||r.status)+'</span></div>'+pedido+ref+instantNote+'<div class="note"><strong>Categoria sugerida:</strong> '+esc(M[r.suggested_group_key]||r.suggested_group_key)+'</div>'+(r.notes?'<div class="note"><strong>Observação:</strong> '+esc(r.notes)+'</div>':'')+'<div class="request-grid"><div><label>Termo que o Auvello pesquisará</label><input data-f="search" maxlength="180" value="'+esc(search)+'" placeholder="Digite manualmente se o usuário enviou só o link"></div><div><label>Categoria validada</label><select data-f="group">'+activeOptions(group)+'</select></div></div><div class="community-actions"><button class="secondary" data-status="em_analise">Em análise</button><button data-status="aprovado">Aprovar</button><button class="secondary" data-status="precisa_contato">Precisa contato</button><button class="danger" data-status="rejeitado">Rejeitar</button></div></div>';}).join('');$('requests').querySelectorAll('[data-request]').forEach(card=>card.querySelectorAll('[data-status]').forEach(b=>b.onclick=()=>updateRequest(card,b.dataset.status)));}catch(e){$('requests').innerHTML='<div class="empty">Erro: '+esc(e.message)+'</div>';}}
async function updateRequest(card,status){const search=card.querySelector('[data-f="search"]').value.trim(),groupKey=card.querySelector('[data-f="group"]').value;const button=card.querySelector('[data-status="'+status+'"]');const original=button?.textContent;if(button&&status==='aprovado'){button.disabled=true;button.textContent='Consultando...';}try{const d=await api('/api/admin/community-requests/'+card.dataset.request,{method:'PATCH',body:JSON.stringify({status,approvedSearchTerm:search,approvedGroupKey:groupKey})});if(status==='aprovado')showLookup('Consulta após aprovação — '+search,d.lookup,d.lookup_error);await loadRequests();}catch(e){alert(e.message);}finally{if(button){button.disabled=false;if(original)button.textContent=original;}}}
$('catAdd').onclick=createCategory;$('add').onclick=addProduct;$('refresh').onclick=loadProducts;$('refreshRequests').onclick=loadRequests;(async()=>{await loadWhatsAppGroups();await loadCategories();await loadProducts();await loadRequests();})();
</script></body></html>`);
});

app.get("/api/admin/categories", adminAuth, async (_req, res) => {
  try { res.json({ categories: await listCategories() }); }
  catch (error) { console.error("[admin/categories] list:", error); res.status(500).json({ error: "Não foi possível listar as categorias." }); }
});

function normalizeCategorySearchTerms(value) {
  const raw = String(value || "");
  const parts = raw.replace(/\r\n?/g, "\n").split(/[\n,;]+/);
  const seen = new Set();
  const terms = [];
  for (const part of parts) {
    const term = part.replace(/\s+/g, " ").trim().slice(0, 120);
    const key = term.toLocaleLowerCase("pt-BR");
    if (!term || seen.has(key)) continue;
    seen.add(key);
    terms.push(term);
    if (terms.length >= 15) break;
  }
  return terms.join("\n");
}

app.post("/api/admin/categories", adminAuth, async (req, res) => {
  const name = normalizeText(req.body?.name, 100);
  const searchTerm = normalizeCategorySearchTerms(req.body?.searchTerm) || null;
  const rawGroupId = String(req.body?.whatsappGroupId || "").trim();
  const whatsappGroupId = rawGroupId ? normalizeGroupId(rawGroupId) : null;
  const publicVisible = req.body?.publicVisible !== false;
  const mirrorToGeneral = req.body?.mirrorToGeneral !== false;
  if (name.length < 2) return res.status(400).json({ error: "Informe um nome para a categoria." });
  if (rawGroupId && !whatsappGroupId) return res.status(400).json({ error: "Grupo do WhatsApp inválido." });
  const groupKey = slugifyGroupKey(name);
  if (!groupKey || groupKey === "geral") return res.status(400).json({ error: "Nome de categoria inválido ou reservado." });
  try {
    await ensureAdminTable();
    const { rows } = await adminPool.query(
      `INSERT INTO auvello_categories (group_key,name,whatsapp_group_id,search_term,active,public_visible,mirror_to_general,created_at,updated_at)
       VALUES ($1,$2,$3,$4,TRUE,$5,$6,NOW(),NOW()) RETURNING *`,
      [groupKey,name,whatsappGroupId,searchTerm,publicVisible,mirrorToGeneral]
    );
    res.status(201).json({ category: rows[0] });
  } catch (error) {
    if (error?.code === "23505") return res.status(409).json({ error: "Já existe uma categoria com esse nome/chave." });
    console.error("[admin/categories] create:", error); res.status(500).json({ error: "Não foi possível criar a categoria." });
  }
});

app.patch("/api/admin/categories/:id", adminAuth, async (req, res) => {
  const id = Number(req.params.id); if (!Number.isInteger(id)||id<=0) return res.status(400).json({ error: "ID inválido." });
  const searchTerm = normalizeCategorySearchTerms(req.body?.searchTerm)||null;
  const rawGroupId = String(req.body?.whatsappGroupId||"").trim(); const whatsappGroupId=rawGroupId?normalizeGroupId(rawGroupId):null;
  if(rawGroupId&&!whatsappGroupId)return res.status(400).json({error:"Grupo do WhatsApp inválido."});
  try{await ensureAdminTable();const {rows}=await adminPool.query(`UPDATE auvello_categories SET search_term=$2,whatsapp_group_id=$3,active=$4,public_visible=$5,mirror_to_general=$6,updated_at=NOW() WHERE id=$1 RETURNING *`,[id,searchTerm,whatsappGroupId,Boolean(req.body?.active),Boolean(req.body?.publicVisible),Boolean(req.body?.mirrorToGeneral)]);if(!rows[0])return res.status(404).json({error:"Categoria não encontrada."});res.json({category:rows[0]});}catch(error){console.error("[admin/categories] update:",error);res.status(500).json({error:"Não foi possível atualizar a categoria."});}
});

app.delete("/api/admin/categories/:id", adminAuth, async (req,res)=>{
  const id=Number(req.params.id);if(!Number.isInteger(id)||id<=0)return res.status(400).json({error:"ID inválido."});
  try{await ensureAdminTable();const {rows}=await adminPool.query("SELECT group_key FROM auvello_categories WHERE id=$1 LIMIT 1",[id]);if(!rows[0])return res.status(404).json({error:"Categoria não encontrada."});const key=rows[0].group_key;const used=await adminPool.query(`SELECT (SELECT COUNT(*) FROM admin_monitored_products WHERE group_key=$1) + (SELECT COUNT(*) FROM community_requests WHERE suggested_group_key=$1 OR approved_group_key=$1) AS total`,[key]);if(Number(used.rows[0]?.total||0)>0)return res.status(409).json({error:"Esta categoria já está em uso. Pause-a em vez de excluir, ou remova/mova os registros associados."});await adminPool.query("DELETE FROM auvello_categories WHERE id=$1",[id]);res.json({ok:true});}catch(error){console.error("[admin/categories] delete:",error);res.status(500).json({error:"Não foi possível excluir a categoria."});}
});

app.get("/api/admin/whatsapp-groups", adminAuth, async (_req,res)=>{
  if(!ready||!sock)return res.json({groups:[]});
  try{const groups=await sock.groupFetchAllParticipating();res.json({groups:Object.values(groups).map(g=>({id:g.id,subject:g.subject})).sort((a,b)=>a.subject.localeCompare(b.subject,'pt-BR'))});}catch(error){res.status(500).json({error:"Não foi possível listar os grupos do WhatsApp."});}
});

app.get("/api/admin/products", adminAuth, async (_req, res) => {
  try { await ensureAdminTable(); const {rows}=await adminPool.query(`SELECT a.id,a.product_id,a.group_key,a.active,a.created_at,a.updated_at,lastn.price AS last_price,lastn.discount_percent AS last_discount,lastn.sent_at AS last_sent_at FROM admin_monitored_products a LEFT JOIN LATERAL (SELECT price,discount_percent,sent_at FROM product_notifications WHERE catalog_product_key=a.product_id AND group_key=a.group_key ORDER BY sent_at DESC LIMIT 1) lastn ON TRUE ORDER BY a.created_at DESC`);res.json({products:rows.map(r=>({...r,in_static_watchlist:isInStaticWatchlist(r.product_id)}))}); }
  catch(error){console.error("[admin] list:",error);res.status(500).json({error:"Não foi possível listar os produtos."});}
});

app.post("/api/admin/products", adminAuth, async (req,res)=>{
  const productId=extractProductId(req.body?.value);const groupKey=String(req.body?.groupKey||"").trim();if(!productId)return res.status(400).json({error:"Não encontrei um PRODUCT_ID válido (MLB...) nesse valor."});const category=await getCategoryByKey(groupKey,{activeOnly:true}).catch(()=>null);if(!category)return res.status(400).json({error:"Categoria inválida ou pausada."});
  try{const existing=await adminPool.query("SELECT * FROM admin_monitored_products WHERE product_id=$1 LIMIT 1",[productId]);if(existing.rows[0])return res.status(409).json({error:"Produto já cadastrado no Admin.",existing:existing.rows[0]});const {rows}=await adminPool.query(`INSERT INTO admin_monitored_products(product_id,group_key,active,created_at,updated_at) VALUES($1,$2,TRUE,NOW(),NOW()) RETURNING *`,[productId,groupKey]);res.status(201).json({product:rows[0],inStaticWatchlist:isInStaticWatchlist(productId)});}catch(error){console.error("[admin] create:",error);res.status(500).json({error:"Não foi possível cadastrar o produto."});}
});

app.post("/api/admin/products/:productId/lookup", adminAuth, async (req,res)=>{
  const productId=extractProductId(req.params.productId);
  if(!productId)return res.status(400).json({error:"PRODUCT_ID inválido."});
  try{
    await ensureAdminTable();
    const current=await adminPool.query("SELECT product_id FROM admin_monitored_products WHERE product_id=$1 LIMIT 1",[productId]);
    if(!current.rows[0])return res.status(404).json({error:"Produto fixado não encontrado."});
    const lookup=await runManualLookup({productId});
    console.log(`[manual-lookup] fixado ${productId}: ${lookup.results?.length||0} resultados`);
    res.json({ok:true,lookup});
  }catch(error){
    console.error("[manual-lookup] fixado:",error);
    res.status(502).json({error:"Não foi possível consultar esse produto agora."});
  }
});

app.patch("/api/admin/products/:productId", adminAuth, async (req,res)=>{
  const productId=extractProductId(req.params.productId);if(!productId)return res.status(400).json({error:"PRODUCT_ID inválido."});const active=req.body?.active;const groupKey=req.body?.groupKey;if(active===undefined&&groupKey===undefined)return res.status(400).json({error:"Informe active ou groupKey."});if(groupKey!==undefined){const category=await getCategoryByKey(String(groupKey),{activeOnly:true}).catch(()=>null);if(!category)return res.status(400).json({error:"Categoria inválida ou pausada."});}
  try{const current=await adminPool.query("SELECT * FROM admin_monitored_products WHERE product_id=$1 LIMIT 1",[productId]);if(!current.rows[0])return res.status(404).json({error:"Produto não encontrado."});const newActive=active===undefined?current.rows[0].active:Boolean(active),newGroup=groupKey===undefined?current.rows[0].group_key:String(groupKey);const {rows}=await adminPool.query(`UPDATE admin_monitored_products SET active=$2,group_key=$3,updated_at=NOW() WHERE product_id=$1 RETURNING *`,[productId,newActive,newGroup]);res.json({product:rows[0]});}catch(error){console.error("[admin] update:",error);res.status(500).json({error:"Não foi possível atualizar o produto."});}
});

app.delete("/api/admin/products/:productId", adminAuth, async (req,res)=>{const productId=extractProductId(req.params.productId);if(!productId)return res.status(400).json({error:"PRODUCT_ID inválido."});try{const result=await adminPool.query("DELETE FROM admin_monitored_products WHERE product_id=$1",[productId]);if(!result.rowCount)return res.status(404).json({error:"Produto não encontrado."});res.json({ok:true});}catch(error){res.status(500).json({error:"Não foi possível remover o produto."});}});

app.get("/api/admin/community-requests", adminAuth, async (_req,res)=>{try{await ensureAdminTable();const {rows}=await adminPool.query(`SELECT id,name,whatsapp,reference_url,reference_product_id,desired_item,suggested_group_key,approved_group_key,approved_search_term,notes,status,instant_lookup_count,instant_response_status,instant_response_error,instant_response_at,created_at,updated_at FROM community_requests WHERE status IN ('pendente','em_analise','precisa_contato') ORDER BY CASE status WHEN 'pendente' THEN 1 WHEN 'em_analise' THEN 2 WHEN 'precisa_contato' THEN 3 ELSE 4 END,created_at DESC LIMIT 150`);res.json({requests:rows});}catch(error){res.status(500).json({error:"Não foi possível listar os pedidos da comunidade."});}});

app.patch("/api/admin/community-requests/:id", adminAuth, async (req,res)=>{
  const id=Number(req.params.id);
  if(!Number.isInteger(id)||id<=0)return res.status(400).json({error:"ID inválido."});
  const allowed=new Set(["pendente","em_analise","aprovado","rejeitado","precisa_contato"]);
  const status=String(req.body?.status||"").trim();
  const approvedSearchTerm=normalizeText(req.body?.approvedSearchTerm,180);
  const approvedGroupKey=String(req.body?.approvedGroupKey||"").trim();
  if(!allowed.has(status))return res.status(400).json({error:"Status inválido."});

  try{
    await ensureAdminTable();
    const current=await adminPool.query("SELECT * FROM community_requests WHERE id=$1 LIMIT 1",[id]);
    if(!current.rows[0])return res.status(404).json({error:"Solicitação não encontrada."});

    const currentRow=current.rows[0];

    // Estados que não são aprovação não precisam validar termo/categoria.
    // Isso garante que Rejeitar/Em análise/Precisa contato funcionem mesmo
    // quando o pedido foi enviado apenas com link.
    if(status!=="aprovado"){
      const {rows}=await adminPool.query(`UPDATE community_requests SET status=$2,updated_at=NOW() WHERE id=$1 RETURNING *`,[id,status]);
      console.log(`[community] #${id} -> ${status}`);
      return res.json({request:rows[0],lookup:null,lookup_error:null});
    }

    const referenceProductId=extractProductId(currentRow.reference_url)||currentRow.reference_product_id||null;
    const urlSearchTerm=searchTermFromReferenceUrl(currentRow.reference_url);
    let finalSearch=approvedSearchTerm||currentRow.approved_search_term||currentRow.desired_item||urlSearchTerm||"";
    if(looksLikeUrl(finalSearch)) finalSearch=urlSearchTerm;
    const finalGroup=approvedGroupKey||currentRow.approved_group_key||currentRow.suggested_group_key;
    const category=await getCategoryByKey(finalGroup,{activeOnly:true});

    if(!finalSearch||!category)return res.status(400).json({error:"Para aprovar, informe um produto/termo válido e uma categoria ativa."});

    const dup=await adminPool.query(`SELECT id FROM community_requests WHERE id<>$1 AND status='aprovado' AND LOWER(TRIM(approved_search_term))=LOWER(TRIM($2)) AND approved_group_key=$3 LIMIT 1`,[id,finalSearch,finalGroup]);
    if(dup.rows[0])return res.status(409).json({error:`Já existe um pedido aprovado com esse mesmo termo e categoria (#${dup.rows[0].id}).`});

    let lookup=null;
    let lookup_error=null;

    try{
      // MLBU é USER_PRODUCT. Para links /up/MLBU..., consultamos pelo nome
      // extraído da própria URL. PRODUCT_IDs MLB de catálogo ainda podem usar
      // consulta direta; se ela vier vazia, caímos para a busca por termo.
      if(referenceProductId && /^MLB\d+$/i.test(referenceProductId) && !/^MLBU/i.test(referenceProductId)){
        lookup=await runManualLookup({productId:referenceProductId});
      }
      if(!lookup?.results?.length){
        lookup=await runManualLookup({term:finalSearch});
      }
      console.log(`[manual-lookup] aprovação #${id} ${finalSearch}: ${lookup.results?.length||0} resultados`);
    }catch(error){
      lookup_error=String(error?.message||error);
      console.error(`[manual-lookup] aprovação #${id}:`,error);
    }

    const {rows}=await adminPool.query(`UPDATE community_requests SET status='aprovado',approved_search_term=$2,approved_group_key=$3,reference_product_id=COALESCE($4,reference_product_id),updated_at=NOW() WHERE id=$1 RETURNING *`,[id,finalSearch,finalGroup,referenceProductId]);
    console.log(`[community] #${id} -> aprovado | ${finalSearch} -> ${finalGroup}`);
    res.json({request:rows[0],lookup,lookup_error});
  }catch(error){
    console.error("[admin/community] update:",error);
    res.status(500).json({error:"Não foi possível atualizar a solicitação."});
  }
});

async function connectWhatsApp(){const db=process.env.DATABASE_URL?.trim();const authProvider=db?await useNeonAuthState(db):await useMultiFileAuthState("auth_info");const{state,saveCreds}=authProvider;console.log(db?"[whatsapp] sessao persistida no PostgreSQL/Neon.":"[whatsapp] DATABASE_URL ausente; usando auth_info local.");sock=makeWASocket({auth:state,logger:pino({level:"silent"}),printQRInTerminal:false,syncFullHistory:false,markOnlineOnConnect:false});sock.ev.on("creds.update",saveCreds);sock.ev.on("connection.update",({connection,lastDisconnect,qr})=>{if(qr){latestQr=qr;console.log("\nQR Code atualizado. Abra /qr?key=SEU_QR_SECRET no navegador.\n");qrcodeTerminal.generate(qr,{small:true});}if(connection==="open"){ready=true;latestQr=null;console.log("WhatsApp conectado.");}if(connection==="close"){ready=false;latestQr=null;const error=lastDisconnect?.error;const statusCode=error instanceof Boom?error.output?.statusCode:undefined;const shouldReconnect=statusCode!==DisconnectReason.loggedOut;console.log("WhatsApp desconectado.",shouldReconnect?"Reconectando...":"Sessao encerrada.");if(shouldReconnect)setTimeout(connectWhatsApp,3000);}});}
app.get("/health",(_req,res)=>res.json({ok:true,whatsappReady:ready}));
app.get("/qr",async(req,res)=>{const configuredSecret=process.env.QR_SECRET?.trim(),supplied=String(req.query.key||"").trim();if(!configuredSecret||supplied!==configuredSecret)return res.status(401).send("Nao autorizado.");if(ready)return res.status(200).type("html").send("<h2>WhatsApp conectado ✅</h2>");if(!latestQr)return res.status(200).type("html").send('<meta http-equiv="refresh" content="3"><h2>Aguardando QR Code...</h2>');try{const dataUrl=await QRCode.toDataURL(latestQr,{errorCorrectionLevel:"M",margin:2,width:420});return res.status(200).type("html").send(`<h2>Conectar WhatsApp ao Auvello</h2><img src="${dataUrl}" style="max-width:100%">`);}catch(error){return res.status(500).send("Nao foi possivel gerar o QR Code.");}});
function lookupDispatchMessage(result){
  const brl=value=>Number(value||0).toLocaleString("pt-BR",{style:"currency",currency:"BRL"});
  const lines=["🔥 *ACHADO AUVELLO*","",`*${String(result.name||result.product_id||"Oferta Auvello").trim()}*`,""];
  const price=Number(result.price||0),original=Number(result.original_price||0),discount=Number(result.discount_percent||0);
  if(original>price&&price>0)lines.push(`De: ~${brl(original)}~`);
  if(price>0)lines.push(`Por: *${brl(price)}*`);
  if(discount>0)lines.push(`💥 *${discount.toFixed(0)}% OFF*`);
  lines.push("","🛒 Link:",String(result.url||"").trim());
  return lines.join("\n");
}

app.post("/api/admin/lookup-dispatch",adminAuth,async(req,res)=>{
  if(!ready||!sock)return res.status(503).json({error:"WhatsApp ainda não está conectado."});
  const groupId=normalizeGroupId(req.body?.groupId);
  const results=Array.isArray(req.body?.results)?req.body.results.slice(0,3):[];
  if(!groupId)return res.status(400).json({error:"Selecione um grupo válido do WhatsApp."});
  if(!results.length)return res.status(400).json({error:"Nenhuma oferta para disparar."});
  if(results.some(r=>!String(r?.url||"").startsWith("http")))return res.status(400).json({error:"Todas as ofertas precisam ter link de afiliado antes do disparo."});
  try{
    const groups=await sock.groupFetchAllParticipating();
    if(!groups[groupId])return res.status(400).json({error:"O grupo selecionado não está disponível nesta sessão do WhatsApp."});
    let sent=0;
    for(const result of results){
      const message=lookupDispatchMessage(result);
      const picture=String(result?.picture||"").trim();
      if(picture)await sock.sendMessage(groupId,{image:{url:picture},caption:message});
      else await sock.sendMessage(groupId,{text:message});
      sent+=1;
    }
    console.log(`[manual-lookup] disparo admin: ${sent} oferta(s) -> ${groupId}`);
    res.json({ok:true,sent,groupId});
  }catch(error){
    console.error("[manual-lookup] disparo:",error);
    res.status(502).json({error:"Não foi possível disparar as ofertas para o WhatsApp."});
  }
});

app.get("/groups",async(_req,res)=>{if(!ready||!sock)return res.status(503).json({error:"WhatsApp ainda nao conectado."});try{const groups=await sock.groupFetchAllParticipating();res.json(Object.values(groups).map(g=>({id:g.id,subject:g.subject})));}catch(error){res.status(500).json({error:String(error)});}});
app.post("/send",async(req,res)=>{if(!ready||!sock)return res.status(503).json({error:"WhatsApp ainda nao conectado."});const{groupId,message,imageUrl}=req.body||{};if(!groupId||!message)return res.status(400).json({error:"Informe groupId e message."});if(!String(groupId).endsWith("@g.us"))return res.status(400).json({error:"groupId deve terminar com @g.us."});try{const result=imageUrl?await sock.sendMessage(groupId,{image:{url:imageUrl},caption:message}):await sock.sendMessage(groupId,{text:message});res.json({ok:true,id:result?.key?.id||null});}catch(error){res.status(500).json({error:String(error)});}});

const PORT=process.env.PORT||3000;app.listen(PORT,async()=>{console.log(`Auvello WhatsApp Service na porta ${PORT}`);await connectWhatsApp();});
