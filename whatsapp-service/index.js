import express from "express";
import qrcodeTerminal from "qrcode-terminal";
import QRCode from "qrcode";
import pino from "pino";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import { useNeonAuthState } from "./neon-auth.js";

const app = express();
app.use(express.json({ limit: "1mb" }));

let sock = null;
let ready = false;
let latestQr = null;
let reconnectTimer = null;

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
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      latestQr = qr;
      console.log("\nQR Code atualizado. Abra /qr?key=SEU_QR_SECRET no navegador.\n");
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
      const statusCode =
        error instanceof Boom ? error.output?.statusCode : undefined;

      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      console.log(
        "WhatsApp desconectado.",
        shouldReconnect ? "Reconectando..." : "Sessao encerrada."
      );

      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }

      if (shouldReconnect) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connectWhatsApp().catch((err) =>
            console.error("[whatsapp] falha ao reconectar:", err)
          );
        }, 3000);
      }
    }
  });
}

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    whatsappReady: ready,
  });
});

app.get("/qr", async (req, res) => {
  const configuredSecret = process.env.QR_SECRET?.trim();
  const supplied = String(req.query.key || "").trim();

  if (!configuredSecret || supplied !== configuredSecret) {
    return res.status(401).send("Nao autorizado.");
  }

  if (ready) {
    return res.status(200).type("html").send("<h2>WhatsApp conectado ✅</h2>");
  }

  if (!latestQr) {
    return res
      .status(200)
      .type("html")
      .send(
        '<meta http-equiv="refresh" content="3"><h2>Aguardando QR Code...</h2>'
      );
  }

  try {
    const dataUrl = await QRCode.toDataURL(latestQr, {
      errorCorrectionLevel: "M",
      margin: 2,
      width: 420,
    });

    return res.status(200).type("html").send(
      `<h2>Conectar WhatsApp ao Auvello</h2>
       <img src="${dataUrl}" style="max-width:100%">`
    );
  } catch (_error) {
    return res.status(500).send("Nao foi possivel gerar o QR Code.");
  }
});

app.get("/groups", async (_req, res) => {
  if (!ready || !sock) {
    return res.status(503).json({
      error: "WhatsApp ainda nao conectado.",
    });
  }

  try {
    const groups = await sock.groupFetchAllParticipating();

    return res.json(
      Object.values(groups).map((group) => ({
        id: group.id,
        subject: group.subject,
      }))
    );
  } catch (error) {
    return res.status(500).json({
      error: String(error),
    });
  }
});

app.post("/send", async (req, res) => {
  if (!ready || !sock) {
    return res.status(503).json({
      error: "WhatsApp ainda nao conectado.",
    });
  }

  const { groupId, message, imageUrl } = req.body || {};

  if (!groupId || !message) {
    return res.status(400).json({
      error: "Informe groupId e message.",
    });
  }

  if (!String(groupId).endsWith("@g.us")) {
    return res.status(400).json({
      error: "groupId deve terminar com @g.us.",
    });
  }

  try {
    const result = imageUrl
      ? await sock.sendMessage(groupId, {
          image: { url: imageUrl },
          caption: message,
        })
      : await sock.sendMessage(groupId, {
          text: message,
        });

    return res.json({
      ok: true,
      id: result?.key?.id || null,
    });
  } catch (error) {
    console.error("[whatsapp/send]", error);

    return res.status(500).json({
      error: String(error),
    });
  }
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, async () => {
  console.log(`Auvello WhatsApp Service na porta ${PORT}`);

  try {
    await connectWhatsApp();
  } catch (error) {
    console.error("[whatsapp] falha ao iniciar:", error);
  }
});
