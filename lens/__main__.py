"""Container/CLI entrypoint for GroundWork Lens."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from lens.app import create_app
from lens.wsgi import QuietWSGIHandler, ThreadingWSGIServer


def main() -> int:
    host = os.environ.get("LENS_HOST", "0.0.0.0")
    port = int(os.environ.get("LENS_PORT", "8080"))
    data_dir = Path(os.environ.get("LENS_DATA_DIR", "./data")).resolve()

    app = create_app(data_dir=data_dir, start_worker=True)
    ctx = app.ctx  # type: ignore[attr-defined]
    if ctx.setup_code:
        print("=" * 60, flush=True)
        print("GroundWork Lens first-run setup code:", ctx.setup_code, flush=True)
        print("=" * 60, flush=True)

    server = ThreadingWSGIServer((host, port), app, handler_class=QuietWSGIHandler)
    print(f"GroundWork Lens listening on http://{host}:{port}", flush=True)

    def _shutdown(signum: int, frame: object) -> None:
        ctx.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
