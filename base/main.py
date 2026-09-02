"""Base station entrypoint: mixer + orchestrator WS (:8800) + dashboard HTTP
(:8080), one process. `python3 -m base.main --roster roster.yaml` or
`make base` for a demo roster."""
from __future__ import annotations
import argparse
import asyncio
import os

from common.protocol import CONTROL_PORT
from common.roster import load_roster, demo_roster
from base.mixer.pymixer import PyMixer
from base.orc.server import Orchestrator

STATIC = os.path.join(os.path.dirname(__file__), "ui", "static")
UI_PORT = 8080


async def _serve_static(host: str = "0.0.0.0", port: int = UI_PORT):
    """Minimal HTTP static server (stdlib-only; the page is one file)."""
    async def client(reader, writer):
        try:
            req = await asyncio.wait_for(reader.readline(), 5)
            path = req.split()[1].decode() if len(req.split()) > 1 else "/"
            while (await asyncio.wait_for(reader.readline(), 5)).strip():
                pass
            fn = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, fn))
            if full.startswith(STATIC) and os.path.isfile(full):
                body = open(full, "rb").read()
                ctype = "text/html" if fn.endswith(".html") else "application/octet-stream"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode()
                             + b"\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\nCache-Control: no-store\r\n\r\n" + body)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    return await asyncio.start_server(client, host, port)


async def main(roster_path: str | None):
    roster = load_roster(roster_path) if roster_path else demo_roster(6, include_music=True)
    mixer = PyMixer()
    orc = Orchestrator(roster, mixer)
    await mixer.start()
    ws = await orc.serve("0.0.0.0", CONTROL_PORT)
    http = await _serve_static()
    print(f"base up: mixer rtp:{mixer.rtp_port}  control ws:{CONTROL_PORT}  "
          f"dashboard http:{UI_PORT}  riders: {', '.join(roster.riders)}")
    try:
        await asyncio.Event().wait()
    finally:
        mixer.stop(); ws.close(); http.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", default=None)
    args = ap.parse_args()
    asyncio.run(main(args.roster))
