import asyncio
import os
from pathlib import Path
import sys

import uvicorn


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    base_dir = Path(__file__).resolve().parent
    load_env_file(base_dir / ".env")

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "2010"))
    workers = max(1, int(os.getenv("APP_WORKERS", "1")))
    cert_file = os.getenv("SSL_CERT_FILE", "./certs/fullchain.pem")
    key_file = os.getenv("SSL_KEY_FILE", "./certs/privkey.pem")

    cert_path = (base_dir / cert_file).resolve() if not Path(cert_file).is_absolute() else Path(cert_file)
    key_path = (base_dir / key_file).resolve() if not Path(key_file).is_absolute() else Path(key_file)

    if not cert_path.exists():
        raise FileNotFoundError(f"SSL cert not found: {cert_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"SSL key not found: {key_path}")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=workers,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
