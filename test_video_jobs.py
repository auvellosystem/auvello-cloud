import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import settings
from app.database import Database
from app.video_jobs import VideoJobs, VideoDelivery
from test_videos import product


class VideoJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch('app.database.settings', replace(settings, database_url='', database_path=str(Path(self.tmp.name) / 'test.db')))
        self.patch.start()
        self.db = Database()
        self.jobs = VideoJobs(self.db)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_persisted_blob_and_dedup_after_restart(self):
        key = self.jobs.reserve(product(), '5528999676818', 1)
        self.jobs.ready(key, b'video bytes', 'Descrição #ofertas')
        restarted = VideoJobs(Database())
        self.assertEqual(bytes(restarted.pending()[0]['video']), b'video bytes')
        self.assertIsNone(restarted.reserve(product(), '5528999676818', 1))
        self.assertIsNone(restarted.reserve(product('two'), '5528999676818', 1))
        restarted.acknowledge(key, 'video', 'vid1')
        restarted.acknowledge(key, 'caption', 'text1')
        self.assertEqual(restarted.pending(), [])
        with self.db._connect() as conn:
            self.assertIsNone(conn.execute('SELECT video FROM auvello_video_jobs').fetchone()['video'])

    def test_caption_retry_does_not_resend_video(self):
        key = self.jobs.reserve(product(), '5528999676818', 3)
        self.jobs.ready(key, b'video', '#achadinhos link')
        client = Mock()
        client.send_video_asset.side_effect = [{'id': 'vid1'}, None]
        VideoDelivery(self.db, client, Mock()).deliver_pending()
        self.assertEqual(self.jobs.pending()[0]['video_sent'], 1)
        retry = Mock()
        retry.send_video_asset.return_value = {'id': 'text1'}
        VideoDelivery(Database(), retry, Mock()).deliver_pending()
        self.assertEqual(retry.send_video_asset.call_count, 1)
        self.assertEqual(retry.send_video_asset.call_args.args[1], 'caption')
        self.assertEqual(self.jobs.pending(), [])

    def test_failed_render_releases_daily_slot(self):
        key = self.jobs.reserve(product(), '5528999676818', 1)
        self.jobs.fail(key)
        self.assertIsNotNone(self.jobs.reserve(product('two'), '5528999676818', 1))

    def test_http_contract(self):
        from app.whatsapp import WhatsAppClient
        response = Mock(ok=True)
        response.json.return_value = {'ok': True, 'id': 'message1'}
        with patch.dict('os.environ', {'WHATSAPP_API_SECRET': 'test-secret'}), patch('app.whatsapp.requests.post', return_value=response) as post:
            job = {'recipient': '5528999676818', 'job_key': 'a' * 24, 'video': b'bytes', 'caption': '#ofertas'}
            self.assertEqual(WhatsAppClient().send_video_asset(job, 'video')['id'], 'message1')
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['phone'], '5528999676818')
            self.assertIn('videoBase64', payload)
            self.assertEqual(post.call_args.kwargs['headers']['x-auvello-key'], 'test-secret')


if __name__ == '__main__':
    unittest.main()
