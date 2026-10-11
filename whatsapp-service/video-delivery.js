import { createHash } from 'node:crypto';

export const DEFAULT_VIDEO_PHONE = '5528999676818';

export async function resolveVideoDestination(sock, phone) {
  const normalize = jid => String(jid || '').replace(/:\d+(?=@)/, '');
  const self = normalize(sock.user?.id);
  const requested = `${phone}@s.whatsapp.net`;
  if (self === requested) return self;
  const matches = await sock.onWhatsApp(requested);
  const destination = matches?.find(item => item.exists)?.jid;
  if (!destination) throw new Error('Número não encontrado no WhatsApp.');
  return normalize(destination) === self ? self : destination;
}

export function createVideoHandler({ authorized, getSocket, pool, recipient = DEFAULT_VIDEO_PHONE }) {
  const completed = new Map();
  const inFlight = new Set();
  let schemaReady;
  return async (req, res) => {
    if (!authorized(req)) return res.status(401).json({ error: 'Não autorizado.' });
    const sock = getSocket();
    if (!sock) return res.status(503).json({ error: 'WhatsApp ainda não conectado.' });
    const { phone, jobKey, phase, videoBase64, message } = req.body || {};
    if (phone !== recipient || !/^55\d{10,11}$/.test(phone) || !/^[a-f0-9]{24}$/.test(jobKey || '')
        || !['video', 'caption'].includes(phase)) {
      return res.status(400).json({ error: 'Destino ou identificador inválido.' });
    }
    let content;
    if (phase === 'video') {
      if (typeof videoBase64 !== 'string' || videoBase64.length > 21 * 1024 * 1024
          || !/^[A-Za-z0-9+/]+={0,2}$/.test(videoBase64)) {
        return res.status(400).json({ error: 'MP4 inválido.' });
      }
      const buffer = Buffer.from(videoBase64, 'base64');
      if (buffer.length > 15 * 1024 * 1024 || buffer.length < 12 || buffer.toString('ascii', 4, 8) !== 'ftyp') {
        return res.status(400).json({ error: 'MP4 inválido ou maior que 15 MB.' });
      }
      content = { document: buffer, mimetype: 'video/mp4', fileName: `auvello-${jobKey}.mp4`,
                  caption: 'Vídeo pronto para publicar. A descrição será enviada em seguida.' };
    } else {
      if (typeof message !== 'string' || !message.trim() || message.length > 12000) {
        return res.status(400).json({ error: 'Descrição inválida.' });
      }
      content = { text: message };
    }
    const key = `${phone}:${jobKey}:${phase}`;
    // Reuse this ID on ambiguous network retries, in addition to the durable ledger.
    const messageId = '3EB0' + createHash('sha256').update(key).digest('hex').slice(0, 28).toUpperCase();
    let client;
    if (inFlight.has(key)) return res.status(409).json({ error: 'Envio em andamento.' });
    inFlight.add(key);
    try {
      if (pool) {
        schemaReady ||= pool.query(`CREATE TABLE IF NOT EXISTS auvello_video_deliveries (
          delivery_key TEXT PRIMARY KEY, message_id TEXT NOT NULL, sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`)
          .catch(error => { schemaReady = undefined; throw error; });
        await schemaReady;
        client = await pool.connect();
        await client.query('BEGIN');
        await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [key]);
        const previous = await client.query('SELECT message_id FROM auvello_video_deliveries WHERE delivery_key=$1', [key]);
        if (previous.rowCount) {
          await client.query('COMMIT');
          return res.json({ ok: true, id: previous.rows[0].message_id, duplicate: true });
        }
      } else if (completed.has(key)) {
        return res.json({ ok: true, id: completed.get(key), duplicate: true });
      }
      const destination = await resolveVideoDestination(sock, phone);
      const result = await sock.sendMessage(destination, content, { messageId, mediaUploadTimeoutMs: 120000 });
      const id = result?.key?.id;
      if (!id) throw new Error('Envio sem confirmação.');
      if (client) {
        await client.query('INSERT INTO auvello_video_deliveries (delivery_key, message_id) VALUES ($1,$2)', [key, id]);
        await client.query('COMMIT');
      } else {
        completed.set(key, id);
      }
      return res.json({ ok: true, id });
    } catch (_error) {
      if (client) await client.query('ROLLBACK').catch(() => {});
      return res.status(503).json({ error: 'Envio adiado; tente novamente na próxima rodada.' });
    } finally {
      client?.release();
      inFlight.delete(key);
    }
  };
}
