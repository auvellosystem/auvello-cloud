import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app.models import Product
from app.videos import VideoGenerator, caption


def product(identifier='one'):
    return Product(identifier, identifier, 'Oferta com acentuação', None, None,
                   80, 100, 'BRL', 'https://example.com', picture='https://example.com/image.jpg')


class VideoTests(unittest.TestCase):
    def test_caption_discount_and_disclosure(self):
        text = caption(product(), 'https://example.com/affiliate')
        self.assertIn('20% de desconto', text)
        self.assertIn('R$ 80,00', text)
        self.assertIn('comissão', text)
        self.assertIn('https://example.com/affiliate', text)

    def test_restart_dedup_and_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            def render(args, **kwargs):
                Path(args[-1]).write_bytes(b'fake video for control test')
            with patch('app.videos.subprocess.run', side_effect=render):
                first = VideoGenerator(tmp, daily_limit=1)
                result = first.generate(product(), product().permalink, picture=Image.new('RGB', (10, 10)))
                self.assertTrue((result / 'legenda.txt').is_file())
                restarted = VideoGenerator(tmp, daily_limit=1)
                self.assertFalse(restarted.eligible(product()))
                self.assertIsNone(restarted.generate(product(), product().permalink))
                self.assertIsNone(restarted.generate(product('two'), product().permalink))

    def test_render_failure_does_not_mark_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            generator = VideoGenerator(tmp, daily_limit=1)
            with patch('app.videos.subprocess.run', side_effect=RuntimeError('failed')):
                with self.assertRaises(RuntimeError):
                    generator.generate(product(), product().permalink, picture=Image.new('RGB', (10, 10)))
            with sqlite3.connect(Path(tmp) / 'controle.sqlite') as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM generated').fetchone()[0], 0)
            self.assertTrue(generator.eligible(product()))


if __name__ == '__main__':
    unittest.main()
