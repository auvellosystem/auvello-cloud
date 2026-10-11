import test from 'node:test';
import assert from 'node:assert/strict';
import { createVideoHandler, resolveVideoDestination, DEFAULT_VIDEO_PHONE } from './video-delivery.js';

function response() {
  return { code: 200, status(code) { this.code = code; return this; }, json(body) { this.body = body; return this; } };
}
function socket() {
  return { user: { id: `${DEFAULT_VIDEO_PHONE}:12@s.whatsapp.net` },
    calls: [], onWhatsApp() { throw Error('Self send should not require lookup'); },
    async sendMessage(jid, content, options) { this.calls.push({ jid, content, options }); return { key: { id: options.messageId } }; } };
}
const base = { phone: DEFAULT_VIDEO_PHONE, jobKey: 'a'.repeat(24) };
const video = Buffer.from([0,0,0,20,...Buffer.from('ftypisom0000')]).toString('base64');

test('own number resolves to normalized self JID', async () => {
  assert.equal(await resolveVideoDestination(socket(), DEFAULT_VIDEO_PHONE), `${DEFAULT_VIDEO_PHONE}@s.whatsapp.net`);
});
test('video preserves MP4 bytes and caption is a separate message; duplicate suppressed', async () => {
  const sock = socket();
  const handler = createVideoHandler({ authorized: () => true, getSocket: () => sock });
  let res = response();
  await handler({ body: { ...base, phase: 'video', videoBase64: video } }, res);
  assert.equal(res.code, 200);
  assert.equal(sock.calls[0].content.mimetype, 'video/mp4');
  assert.equal(sock.calls[0].content.document.toString('base64'), video);
  await handler({ body: { ...base, phase: 'video', videoBase64: video } }, response());
  assert.equal(sock.calls.length, 1);
  await handler({ body: { ...base, phase: 'caption', message: 'Descrição #ofertas https://example.com' } }, response());
  assert.equal(sock.calls.length, 2);
  assert.equal(sock.calls[1].content.text, 'Descrição #ofertas https://example.com');
  assert.notEqual(sock.calls[0].options.messageId, sock.calls[1].options.messageId);
});
test('unauthorized, wrong destination, disconnected socket and invalid media are rejected', async () => {
  for (const [authorized, sock, body, expected] of [
    [false, socket(), base, 401], [true, null, base, 503],
    [true, socket(), { ...base, phone: '5511999999999', phase: 'caption', message: 'hi' }, 400],
    [true, socket(), { ...base, phase: 'video', videoBase64: 'not video' }, 400],
  ]) {
    const res = response();
    await createVideoHandler({ authorized: () => authorized, getSocket: () => sock })({ body }, res);
    assert.equal(res.code, expected);
    if (sock) assert.equal(sock.calls.length, 0);
  }
});
test('network retry keeps the same message ID', async () => {
  const sock = socket(); let attempted;
  sock.sendMessage = async (_jid, _content, options) => {
    if (!attempted) { attempted = options.messageId; throw Error('network'); }
    assert.equal(options.messageId, attempted);
    return { key: { id: attempted } };
  };
  const handler = createVideoHandler({ authorized: () => true, getSocket: () => sock });
  const req = { body: { ...base, phase: 'caption', message: '#ofertas' } };
  let res = response(); await handler(req, res); assert.equal(res.code, 503);
  res = response(); await handler(req, res); assert.equal(res.code, 200);
});
