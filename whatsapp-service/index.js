import express from "express";
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
