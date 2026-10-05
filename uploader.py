"""Tiny upload server so the phone can POST recordings directly (no Syncthing).

POST /upload?t=<token>   (or with an Authorization header)
  Authorization: Bearer <NOTEINATOR_UPLOAD_TOKEN>
  X-Filename:    memo.m4a                      (optional)
  X-Recorded-At: 2026-10-04T14:22:31-05:00     (optional, ISO 8601)
  body:          raw audio bytes

GET /health -> {"ok": true}
GET /setup  -> phone-friendly setup page: tap to copy the token, no typing

Files are written as a hidden temp file, then renamed into the watch folder,
so the existing watcher picks them up exactly like a Syncthing delivery.
"""
import os
import re
import sys
import hmac
import json
import time
import uuid
import logging
import threading
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("Noteinator")


def _safe_name(raw: str, allowed_exts) -> str:
    name = Path(raw or "").name
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', name).lstrip('._')
    stem, ext = os.path.splitext(name)
    if not ext:
        ext = ".m4a"  # iOS "Record Audio" produces m4a
    if ext.lower() not in allowed_exts:
        raise ValueError(f"unsupported file type '{ext}'")
    stem = (stem or "note")[:80]
    return f"{datetime.now():%Y%m%d-%H%M%S}_{stem}{ext.lower()}"


SETUP_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Noteinator setup</title>
<style>
  :root {{ color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif;
          padding: env(safe-area-inset-top,0) 0 env(safe-area-inset-bottom,0); }}
  body {{ margin: 0; padding: 1.5rem; line-height: 1.5; max-width: 34rem; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  p.sub {{ margin: 0 0 1.5rem; opacity: .7; }}
  .field {{ margin-bottom: 1rem; }}
  .label {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; opacity: .6; }}
  button.copy {{ display: block; width: 100%; text-align: left; margin-top: .3rem;
    font: inherit; font-family: ui-monospace, Menlo, monospace; font-size: .95rem;
    word-break: break-all; padding: .8rem 1rem; border-radius: .6rem;
    border: 1px solid rgba(128,128,128,.4); background: rgba(128,128,128,.1);
    color: inherit; cursor: pointer; }}
  button.copy:active {{ background: rgba(128,128,128,.25); }}
  .hint {{ font-size: .8rem; opacity: .6; margin-top: .2rem; }}
  ol {{ padding-left: 1.2rem; }} li {{ margin-bottom: .6rem; }}
  code {{ background: rgba(128,128,128,.15); padding: .1rem .35rem; border-radius: .3rem; }}
  #toast {{ position: fixed; left: 50%; bottom: calc(1.5rem + env(safe-area-inset-bottom,0));
    transform: translateX(-50%); background: #222; color: #fff; padding: .6rem 1.1rem;
    border-radius: 2rem; font-size: .9rem; opacity: 0; transition: opacity .2s;
    pointer-events: none; }}
  #toast.show {{ opacity: .95; }}
</style></head><body>
<h1>Noteinator setup</h1>
<p class="sub">Tap a value to copy it, then paste into the Shortcut.</p>

<div class="field"><div class="label">Upload URL (includes your token)</div>
  <button class="copy" data-v="{url}?t={token}">{url}?t={token_masked}</button>
  <div class="hint">Tap to copy the whole thing. This is the only value you need.</div></div>

<h2 style="font-size:1.1rem">Build the Shortcut</h2>
<ol>
  <li><b>Record Audio</b> — Start: Immediately, Finish: On Tap</li>
  <li><b>Save File</b> — to a <code>Noteinator/</code> folder, Ask Where to Save off</li>
  <li><b>Format Date</b> — Current Date, ISO 8601, include time</li>
  <li><b>Get Contents of URL</b> — paste the URL above, Method <code>POST</code>,
      header <code>X-Recorded-At</code> (the Formatted Date variable),
      Request Body <b>File</b> → Recorded Audio</li>
  <li><b>Show Notification</b> — Contents of URL</li>
</ol>
<p>Name it <b>Lab Note</b>, then assign it to the Action Button or Back Tap.</p>

<div id="toast">Copied</div>
<script>
  const toast = document.getElementById('toast');
  document.querySelectorAll('button.copy').forEach(b => b.onclick = async () => {{
    const v = b.dataset.v;
    try {{ await navigator.clipboard.writeText(v); }}
    catch (e) {{
      const t = document.createElement('textarea');
      t.value = v; document.body.appendChild(t); t.select();
      document.execCommand('copy'); t.remove();
    }}
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 1200);
  }});
</script>
</body></html>"""


def setup_page(token: str, host: str) -> str:
    masked = f"{token[:6]}{'.' * 10}{token[-4:]}" if len(token) > 12 else token
    return SETUP_HTML.format(
        url=f"http://{host}/upload" if host else "http://<server>:8765/upload",
        token=token, token_masked=masked)


def qr_lines(data: str):
    """Terminal QR code, or None if the qrcode lib isn't installed.

    Uses explicit ANSI background colours rather than block characters: a dark-themed
    terminal would otherwise render the code inverted, which phone cameras won't scan.
    Set NOTEINATOR_QR_ASCII=1 to fall back to plain block characters.
    """
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=4)  # 4-module quiet zone is what the spec asks for
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    if os.getenv("NOTEINATOR_QR_ASCII", "") == "1":
        return ["".join("  " if cell else "\u2588\u2588" for cell in row) for row in matrix]

    WHITE, BLACK, RESET = "\x1b[48;5;231m", "\x1b[48;5;16m", "\x1b[0m"
    lines = []
    for row in matrix:
        out = []
        for cell in row:
            out.append((BLACK if cell else WHITE) + "  ")
        lines.append("".join(out) + RESET)
    return lines


def make_handler(token, watch_folder: Path, allowed_exts, max_bytes):

    class UploadHandler(BaseHTTPRequestHandler):
        server_version = "Noteinator"

        def log_message(self, fmt, *args):  # route through our logger
            logger.debug("[Upload] " + fmt % args)

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else ""
            if not given:
                # Also accept ?t=<token> so the Shortcut needs one pasted value, not two.
                # Safe enough on a private tailnet; prefer the header if you expose this.
                given = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        def _read_body(self, out):
            """Stream request body to file; supports Content-Length and chunked."""
            total = 0
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    total += size
                    if total > max_bytes:
                        raise OverflowError
                    out.write(self.rfile.read(size))
                    self.rfile.readline()  # CRLF after chunk
            else:
                remaining = int(self.headers.get("Content-Length", "0"))
                if remaining > max_bytes:
                    raise OverflowError
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
                    total += len(chunk)
            return total

        def do_GET(self):
            route = self.path.split("?")[0].rstrip("/")
            if route == "/health":
                self._reply(200, {"ok": True})
            elif route in ("/setup", ""):
                page = setup_page(token, self.headers.get("Host", "")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/upload":
                return self._reply(404, {"ok": False, "error": "not found"})
            if not self._authorized():
                logger.warning(f"[Upload] Rejected unauthorized request from {self.client_address[0]}")
                return self._reply(401, {"ok": False, "error": "unauthorized"})
            if "multipart/form-data" in self.headers.get("Content-Type", ""):
                return self._reply(400, {"ok": False,
                    "error": "send the audio as the raw request body (Shortcuts: Request Body = File), not a form"})

            try:
                final_name = _safe_name(self.headers.get("X-Filename", ""), allowed_exts)
            except ValueError as e:
                return self._reply(415, {"ok": False, "error": str(e)})

            tmp = watch_folder / f".upload-{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "wb") as f:
                    size = self._read_body(f)
                if size == 0:
                    tmp.unlink(missing_ok=True)
                    return self._reply(400, {"ok": False, "error": "empty body"})

                recorded = self.headers.get("X-Recorded-At", "")
                if recorded:
                    try:
                        ts = datetime.fromisoformat(recorded.strip()).timestamp()
                        os.utime(tmp, (ts, ts))  # becomes recorded_at in the DB
                    except ValueError:
                        logger.warning(f"[Upload] Ignoring unparseable X-Recorded-At: {recorded!r}")

                dest = watch_folder / final_name
                n = 1
                while dest.exists():
                    dest = watch_folder / f"{Path(final_name).stem}_{n}{Path(final_name).suffix}"
                    n += 1
                tmp.rename(dest)  # watcher sees this as a move into the inbox
                logger.info(f"[Upload] Received {dest.name} ({size/1024:.0f} KB) from {self.client_address[0]}")
                self._reply(200, {"ok": True, "file": dest.name})
            except OverflowError:
                tmp.unlink(missing_ok=True)
                self._reply(413, {"ok": False, "error": "file too large"})
            except Exception as e:
                tmp.unlink(missing_ok=True)
                logger.error(f"[Upload Error] {e}")
                self._reply(500, {"ok": False, "error": "server error"})

    return UploadHandler


def start_upload_server(host, port, token, watch_folder, allowed_exts, max_mb):
    handler = make_handler(token, Path(watch_folder), set(allowed_exts), max_mb * 1024 * 1024)
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    setup_url = f"http://{host}:{port}/setup"
    # Only draw the QR for a human at a terminal. Under systemd stdout is a journal
    # pipe, where the colour codes collapse into blank lines and clutter the log.
    lines = qr_lines(setup_url) if sys.stdout.isatty() else None
    if lines:
        print("\n  Scan this on your phone to set up the Shortcut:\n")
        for line in lines:
            print("  " + line)
        print()
        print(f"  Or open it directly: {setup_url}\n")
    elif sys.stdout.isatty():
        print("\n  Open this on your phone to set up the Shortcut "
              "(pip install qrcode for a scannable code here):\n")
        print(f"  {setup_url}\n")
    logger.info(f"[Upload] Listening on http://{host}:{port}/upload")
    logger.info(f"[Upload] Phone setup page: {setup_url}")
    return server
