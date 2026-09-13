import argparse
import time

from app.auth import authorize_interactively
from app.config import settings
from app.service import AuvelloService


def main() -> None:
    parser = argparse.ArgumentParser(description="Auvello")
    parser.add_argument("--auth", action="store_true", help="Refaz OAuth do Mercado Livre")
    parser.add_argument("--once", action="store_true", help="Executa uma rodada e encerra")
    args = parser.parse_args()

    if args.auth:
        authorize_interactively()
        print("Autorizacao concluida.")
        return

    service = AuvelloService()

    if args.once:
        service.run_once()
        return

    interval_minutes = max(1, settings.check_interval_minutes)
    interval_seconds = interval_minutes * 60

    print(f"[scheduler] Auvello iniciado. Nova rodada a cada {interval_minutes} min.")

    while True:
        started_at = time.monotonic()
        try:
            service.run_once()
        except KeyboardInterrupt:
            print("\nEncerrado pelo usuario.")
            return
        except Exception as exc:
            print(f"[ciclo] erro geral: {exc}")

        elapsed = time.monotonic() - started_at
        sleep_seconds = max(1, interval_seconds - elapsed)
        print(
            f"[scheduler] Rodada encerrada. Proxima em "
            f"{sleep_seconds / 60:.1f} min."
        )
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
