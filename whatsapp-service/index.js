import express from "express";
import qrcode from "qrcode-terminal";
import pino from "pino";
import makeWASocket, { DisconnectReason, useMultiFileAuthState } from "@whiskeysockets/baileys";
import { useNeonAuthState } from "./neon-auth.js";
import { Boom } from "@hapi/boom";

const app = express();
app.use(express.json({ limit: "1mb" }));

let sock = null;
let ready = false;

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
      console.log("\nEscaneie o QR Code no WhatsApp:\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      ready = true;
      console.log("WhatsApp conectado.");
    }
    if (connection === "close") {
      ready = false;
      const error = lastDisconnect?.error;
      const statusCode = error instanceof Boom ? error.output?.statusCode : undefined;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log("WhatsApp desconectado.", shouldReconnect ? "Reconectando..." : "Sessao encerrada.");
      if (shouldReconnect) setTimeout(connectWhatsApp, 3000);
    }
  });
}

app.get("/health", (_req, res) => res.json({ ok: true, whatsappReady: ready }));

app.get("/groups", async (_req, res) => {
  if (!ready || !sock) return res.status(503).json({ error: "WhatsApp ainda nao conectado." });
  try {
    const groups = await sock.groupFetchAllParticipating();
    res.json(Object.values(groups).map(g => ({ id: g.id, subject: g.subject })));
  } catch (error) {
    res.status(500).json({ error: String(error) });
  }
});

app.post("/send", async (req, res) => {
  if (!ready || !sock) return res.status(503).json({ error: "WhatsApp ainda nao conectado." });
  const { groupId, message, imageUrl } = req.body || {};
  if (!groupId || !message) return res.status(400).json({ error: "Informe groupId e message." });
  if (!String(groupId).endsWith("@g.us")) return res.status(400).json({ error: "groupId deve terminar com @g.us." });
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
