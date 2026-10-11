import argparse
import time

from app.auth import authorize_interactively
from app.config import settings
from app.service import AuvelloService


def main() -> None:
    parser = argparse.ArgumentParser(description="Auvello")
    parser.add_argument("--auth", action="store_true", help="Refaz OAuth do Mercado Livre")
    parser.add_argument("--once", action="store_true", help="Executa descoberta + específicos + Geral e encerra")
    parser.add_argument("--videos", action="store_true", help="Gera vídeos do cache e envia ao número pessoal, sem enviar aos grupos")
    parser.add_argument("--video-demo", action="store_true", help="Gera vídeo fictício offline para testar")
    args = parser.parse_args()

    if args.video_demo:
        from app.videos import VideoGenerator
        from app.models import Product
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (700, 700), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((200, 80, 500, 620), radius=60, fill="#344b72")
        product = Product("demo", "demo", "DEMONSTRAÇÃO • Produto fictício", None, None,
                          99.90, 149.90, "BRL", "https://example.com")
        print(VideoGenerator(output="videos_demo", daily_limit=1).generate(product, product.permalink, picture=image))
        return

    if args.auth:
        authorize_interactively()
        print("Autorizacao concluida.")
        return

    service = AuvelloService()

    if args.videos:
        service.run_videos()
        return

    if args.once:
        service.run_once()
        return

    discovery_s = max(1, settings.discovery_interval_minutes) * 60
    specific_s = max(1, settings.specific_group_interval_minutes) * 60
    general_s = max(1, settings.general_group_interval_minutes) * 60

    print(
        "[scheduler] Auvello iniciado. "
        f"Descoberta={settings.discovery_interval_minutes} min | "
        f"Especificos={settings.specific_group_interval_minutes} min | "
        f"Geral={settings.general_group_interval_minutes} min"
    )

    # Todos rodam logo na inicialização; a ordem garante cache fresco antes
    # da primeira publicação. Depois cada relógio é independente.
    now = time.monotonic()
    next_discovery = now
    next_specific = now
    next_general = now

    while True:
        try:
            now = time.monotonic()

            if now >= next_discovery:
                try:
                    service.run_discovery()
                except Exception as exc:
                    print(f"[discovery-cycle] erro geral: {exc}")
                next_discovery = time.monotonic() + discovery_s

            now = time.monotonic()
            if now >= next_specific:
                try:
                    service.run_specific_groups()
                except Exception as exc:
                    print(f"[specific-cycle] erro geral: {exc}")
                next_specific = time.monotonic() + specific_s

            now = time.monotonic()
            if now >= next_general:
                try:
                    service.run_general_group()
                except Exception as exc:
                    print(f"[general-cycle] erro geral: {exc}")
                next_general = time.monotonic() + general_s

            now = time.monotonic()
            sleep_seconds = max(1.0, min(next_discovery, next_specific, next_general) - now)
            print(
                "[scheduler] proximos: "
                f"discovery={max(0, next_discovery-now)/60:.1f}m | "
                f"especificos={max(0, next_specific-now)/60:.1f}m | "
                f"geral={max(0, next_general-now)/60:.1f}m"
            )
            time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            print("\nEncerrado pelo usuario.")
            return


if __name__ == "__main__":
    main()
