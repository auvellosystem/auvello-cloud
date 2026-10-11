"""Durable video outbox in the existing SQLite/PostgreSQL database."""
from datetime import datetime, timedelta, timezone
import hashlib
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from .videos import VideoGenerator

DEFAULT_RECIPIENT = '5528999676818'


class VideoJobs:
    def __init__(self, db):
        self.db = db
        blob_type = 'BYTEA' if db.is_postgres else 'BLOB'
        with db._connect() as conn:
            conn.execute(f'''CREATE TABLE IF NOT EXISTS auvello_video_jobs (
                job_key TEXT PRIMARY KEY, product_key TEXT NOT NULL, day TEXT NOT NULL,
                recipient TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL, video {blob_type}, caption TEXT,
                video_sent INTEGER NOT NULL DEFAULT 0, message_id TEXT)''')

    @staticmethod
    def product_key(product):
        return hashlib.sha256(f'{product.marketplace}:{product.item_id or product.product_id}'.encode()).hexdigest()[:24]

    def reserve(self, product, recipient, limit):
        now = datetime.now(timezone.utc)
        day = now.astimezone(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d')
        key = self.product_key(product)
        job = hashlib.sha256(f'{key}:{recipient}'.encode()).hexdigest()[:24]
        ph = self.db._ph
        with self.db._connect() as conn:
            if self.db.is_postgres:
                conn.execute('SELECT pg_advisory_xact_lock(742091)')
            else:
                conn.execute('BEGIN IMMEDIATE')
            # A process killed while rendering may be retried after ten minutes.
            cutoff = (now - timedelta(minutes=10)).isoformat()
            conn.execute(f"UPDATE auvello_video_jobs SET state='failed' WHERE state='rendering' AND updated_at < {ph}", (cutoff,))
            old = conn.execute(f'SELECT state FROM auvello_video_jobs WHERE job_key={ph}', (job,)).fetchone()
            if old and old['state'] != 'failed':
                return None
            count = conn.execute(f"SELECT COUNT(*) AS total FROM auvello_video_jobs WHERE day={ph} AND state!='failed'", (day,)).fetchone()['total']
            if limit <= 0 or count >= limit:
                return None
            conn.execute(f'''INSERT INTO auvello_video_jobs
                (job_key, product_key, day, recipient, state, updated_at, created_at)
                VALUES ({ph},{ph},{ph},{ph},'rendering',{ph},{ph})
                ON CONFLICT(job_key) DO UPDATE SET day=excluded.day, state='rendering',
                updated_at=excluded.updated_at, created_at=excluded.created_at''',
                (job, key, day, recipient, now.isoformat(), now.isoformat()))
            return job

    def ready(self, key, video, caption):
        if len(video) > 15 * 1024 * 1024:
            raise ValueError('Vídeo excede 15 MB.')
        ph = self.db._ph
        with self.db._connect() as conn:
            conn.execute(f"UPDATE auvello_video_jobs SET state='pending', video={ph}, caption={ph}, updated_at={ph} WHERE job_key={ph}",
                         (video, caption, datetime.now(timezone.utc).isoformat(), key))

    def fail(self, key):
        ph = self.db._ph
        with self.db._connect() as conn:
            conn.execute(f"UPDATE auvello_video_jobs SET state='failed', video=NULL WHERE job_key={ph} AND state='rendering'", (key,))

    def pending(self):
        ph = self.db._ph
        with self.db._connect() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            conn.execute(f"UPDATE auvello_video_jobs SET state='expired', video=NULL WHERE state='pending' AND created_at < {ph}", (cutoff,))
            return [dict(row) for row in conn.execute("SELECT * FROM auvello_video_jobs WHERE state='pending' ORDER BY created_at LIMIT 10").fetchall()]

    def acknowledge(self, key, phase, message_id):
        ph = self.db._ph
        with self.db._connect() as conn:
            if phase == 'video':
                conn.execute(f'UPDATE auvello_video_jobs SET video_sent=1, message_id={ph} WHERE job_key={ph}', (message_id, key))
            else:
                conn.execute(f"UPDATE auvello_video_jobs SET state='sent', video=NULL, message_id={ph}, updated_at={ph} WHERE job_key={ph}",
                             (message_id, datetime.now(timezone.utc).isoformat(), key))


class VideoDelivery:
    def __init__(self, db, whatsapp, affiliate):
        self.jobs = VideoJobs(db)
        self.whatsapp = whatsapp
        self.affiliate = affiliate
        self.recipient = os.getenv('VIDEOS_WHATSAPP_PHONE', DEFAULT_RECIPIENT).strip()

    def deliver_pending(self):
        for job in self.jobs.pending():
            try:
                if not job['video_sent']:
                    result = self.whatsapp.send_video_asset(job, 'video')
                    if not result:
                        continue
                    self.jobs.acknowledge(job['job_key'], 'video', result['id'])
                result = self.whatsapp.send_video_asset(job, 'caption')
                if result:
                    self.jobs.acknowledge(job['job_key'], 'caption', result['id'])
                    print('[videos] MP4 e legenda enviados ao WhatsApp configurado.')
            except Exception as exc:
                print(f'[videos] Envio pendente ({type(exc).__name__}); tentará na próxima rodada.')

    def run(self, candidates):
        config = VideoGenerator()
        if not config.enabled:
            return
        if not self.recipient.isdigit() or not self.recipient.startswith('55') or len(self.recipient) not in (12, 13):
            raise ValueError('VIDEOS_WHATSAPP_PHONE deve conter país 55, DDD e número.')
        self.deliver_pending()
        for candidate in candidates:
            product = candidate.product
            if not product.picture or product.currency_id != 'BRL' or not product.price or not math.isfinite(product.price) or product.price <= 0:
                continue
            key = self.jobs.reserve(product, self.recipient, config.daily_limit)
            if not key:
                continue
            try:
                url = self.affiliate(product)
                if not url:
                    raise ValueError('Link de afiliado indisponível.')
                with tempfile.TemporaryDirectory(prefix='auvello-video-') as tmp:
                    generator = VideoGenerator(output=tmp, daily_limit=1)
                    folder = generator.generate(product, url)
                    if not folder:
                        raise RuntimeError('Vídeo não gerado.')
                    self.jobs.ready(key, (folder / 'video.mp4').read_bytes(), (folder / 'legenda.txt').read_text(encoding='utf-8'))
            except Exception as exc:
                self.jobs.fail(key)
                print(f'[videos] Falha na geração ({type(exc).__name__}); demais envios continuam.')
        self.deliver_pending()
