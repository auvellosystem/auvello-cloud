"""Local, free offer videos. No TikTok login or publishing API required."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import Product


def money(value: float) -> str:
    return 'R$ ' + f'{value:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def caption(product: Product, url: str) -> str:
    lines = [f'Confira esta oferta: {product.name}', f'Preço consultado: {money(product.price)}']
    if product.discount_percent:
        lines.append(f'{product.discount_percent:g}% de desconto sobre o preço original informado.')
    lines.extend(['', f'Link da oferta: {url}', 'Link de afiliado: posso receber comissão pela compra.',
                  'Preço e disponibilidade podem mudar. Confira frete e condições na loja.',
                  '', '#ofertas #achadinhos #promocoes'])
    return '\n'.join(lines) + '\n'


def font(size: int, bold: bool = False):
    override = os.getenv('VIDEOS_FONT_BOLD' if bold else 'VIDEOS_FONT', '')
    names = [override, '/usr/share/fonts/truetype/dejavu/DejaVuSans' + ('-Bold' if bold else '') + '.ttf',
             'C:/Windows/Fonts/' + ('arialbd.ttf' if bold else 'arial.ttf'), 'DejaVuSans.ttf']
    for name in names:
        if not name:
            continue
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    raise RuntimeError('Instale uma fonte TrueType ou configure VIDEOS_FONT e VIDEOS_FONT_BOLD.')


def wrapped(draw, value, face, width, max_lines=4):
    lines, line = [], ''
    # Break long tokens as well as ordinary words.
    for char in value:
        trial = line + char
        if draw.textlength(trial, font=face) > width and line:
            split = line.rfind(' ')
            if split > 0:
                lines.append(line[:split])
                line = line[split + 1:] + char
            else:
                lines.append(line)
                line = char
        else:
            line = trial
    if line:
        lines.append(line.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while draw.textlength(lines[-1] + '…', font=face) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '…'
    return lines


class VideoGenerator:
    def __init__(self, output=None, daily_limit=None):
        self.output = Path(output or os.getenv('VIDEOS_OUTPUT_DIR', 'videos_prontos'))
        self.daily_limit = int(daily_limit if daily_limit is not None else os.getenv('VIDEOS_MAX_PER_DAY', '3'))
        self.duration = max(6, min(60, int(os.getenv('VIDEOS_DURATION_SECONDS', '15'))))
        self.enabled = os.getenv('VIDEOS_ENABLED', 'true').lower() in {'1', 'true', 'sim', 'yes', 'on'}

    def eligible(self, product):
        if not self.enabled or self.daily_limit <= 0 or not product.picture:
            return False
        if product.currency_id != 'BRL' or product.price is None or not math.isfinite(product.price) or product.price <= 0:
            return False
        key = hashlib.sha256(f'{product.marketplace}:{product.item_id or product.product_id}'.encode()).hexdigest()[:24]
        day = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d')
        if not (self.output / 'controle.sqlite').exists():
            return True
        with sqlite3.connect(self.output / 'controle.sqlite', timeout=30) as db:
            if db.execute('SELECT 1 FROM generated WHERE product_key=?', (key,)).fetchone():
                return False
            return db.execute('SELECT COUNT(*) FROM generated WHERE day=?', (day,)).fetchone()[0] < self.daily_limit

    def _image(self, url):
        if not url or not url.startswith(('https://', 'http://')):
            raise ValueError('Oferta sem imagem HTTP válida; vídeo ignorado.')
        with requests.get(url, timeout=(10, 25), stream=True) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 15 * 1024 * 1024:
                    raise ValueError('Imagem maior que 15 MB.')
        with Image.open(io.BytesIO(data)) as source:
            return ImageOps.exif_transpose(source).convert('RGB')

    def _card(self, product, picture, stage):
        canvas = Image.new('RGB', (1080, 1920), '#101827')
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((70, 170, 910, 260), radius=32, fill='#39e5ad')
        draw.text((105, 188), ['ACHADO DO DIA', 'CONFIRA O PREÇO', 'GOSTOU DO ACHADO?'][stage], font=font(42, True), fill='#101827')
        y = 310
        for line in wrapped(draw, product.name, font(48, True), 820, 3):
            draw.text((80, y), line, font=font(48, True), fill='white')
            y += 64
        draw.rounded_rectangle((80, 540, 920, 1260), radius=38, fill='white')
        contained = ImageOps.contain(picture, (770, 650), Image.Resampling.LANCZOS)
        canvas.paste(contained, (500 - contained.width // 2, 900 - contained.height // 2))
        if stage == 2:
            title, detail = 'Veja o link na legenda', 'Confira as condições na loja'
        else:
            title = money(product.price)
            detail = (f'{product.discount_percent:g}% OFF' if product.discount_percent else 'Confira esta oferta')
        draw.text((80, 1310), title, font=font(58 if stage == 2 else 88, True), fill='#39e5ad')
        draw.text((80, 1450), detail, font=font(40, True), fill='white')
        draw.text((80, 1570), 'Preço e estoque podem mudar.', font=font(30), fill='#aeb8c8')
        draw.text((80, 1620), 'Conteúdo com link de afiliado.', font=font(30), fill='#aeb8c8')
        return canvas

    def generate(self, product, url, *, picture=None):
        if not self.enabled:
            return None
        if product.currency_id != 'BRL' or product.price is None or not math.isfinite(product.price) or product.price <= 0:
            raise ValueError('Vídeo requer preço positivo em BRL.')
        self.output.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f'{product.marketplace}:{product.item_id or product.product_id}'.encode()).hexdigest()[:24]
        day = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d')
        with sqlite3.connect(self.output / 'controle.sqlite', timeout=30) as db:
            db.execute('CREATE TABLE IF NOT EXISTS generated (product_key TEXT PRIMARY KEY, day TEXT NOT NULL, folder TEXT NOT NULL)')
            # Hold the lock during rendering so two processes cannot exceed the daily limit.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM generated WHERE product_key=?', (key,)).fetchone():
                return None
            if self.daily_limit <= 0 or db.execute('SELECT COUNT(*) FROM generated WHERE day=?', (day,)).fetchone()[0] >= self.daily_limit:
                return None
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                raise RuntimeError('FFmpeg não encontrado. Consulte LEIA-ME-VIDEOS.md.')
            picture = picture if picture is not None else self._image(product.picture)
            destination = self.output / day / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.render-') as tmp:
                work = Path(tmp)
                for stage in range(3):
                    self._card(product, picture, stage).save(work / f'card{stage}.png')
                args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
                for stage in range(3):
                    args += ['-loop', '1', '-i', str(work / f'card{stage}.png')]
                length = self.duration / 3
                filters = [f'[{i}:v]scale=1080:1920,setsar=1,fps=24,trim=duration={length},setpts=PTS-STARTPTS,zoompan=z=1.015:d=1:s=1080x1920:fps=24,fade=t=in:st=0:d=0.25[v{i}]' for i in range(3)]
                filters.append('[v0][v1][v2]concat=n=3:v=1:a=0[out]')
                args += ['-filter_complex', ';'.join(filters), '-map', '[out]', '-t', str(self.duration),
                         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-threads', '2',
                         '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(work / 'video.mp4')]
                subprocess.run(args, check=True, capture_output=True, timeout=300)
                (work / 'legenda.txt').write_text(caption(product, url), encoding='utf-8')
                (work / 'oferta.json').write_text(json.dumps({'produto': product.name, 'preco': product.price,
                    'url': url, 'gerado_em': datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()}, ensure_ascii=False, indent=2), encoding='utf-8')
                for stage in range(3):
                    (work / f'card{stage}.png').unlink()
                if destination.exists():
                    shutil.rmtree(destination)  # Recover an uncommitted output after a crash.
                shutil.move(str(work), str(destination))
                db.execute('INSERT INTO generated VALUES (?, ?, ?)', (key, day, str(destination)))
            return destination
