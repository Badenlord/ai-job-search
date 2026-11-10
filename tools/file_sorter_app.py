#!/usr/bin/env python3
"""File Sorter: a point-and-click window for sort_files.py, fully offline.

    python3 tools/file_sorter_app.py

Your browser opens a page that this script serves from your own computer
only (127.0.0.1). Nothing is uploaded and no internet connection is needed.
Pick a folder, press Scan, check or change where each file will go, then
press Sort. Every sort can be undone from History. Stop it with the Quit
button in the page or Ctrl+C in the terminal.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from tools import sort_files as engine
except ImportError:  # run as a script: tools/ itself is on sys.path
    import sort_files as engine

DEFAULT_PORT = 8765  # fixed so the page's remembered settings survive restarts
MAX_BODY_BYTES = 8_000_000
MAX_SCANS_KEPT = 4
MAX_LISTED_FOLDERS = 2000
ACTIONS = ("places", "browse", "scan", "sort", "history", "undo", "reveal", "quit")


def _folder_arg(body, key="folder"):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise engine.SortError("choose a folder first")
    path = Path(value.strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise engine.SortError(f"folder not found: {value.strip()}") from None
    if not path.is_dir():
        raise engine.SortError(f"not a folder: {path}")
    return path


def _subfolders(folder):
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        raise engine.SortError(f"no permission to open {folder}") from None
    except OSError:
        return []
    return sorted(names, key=str.casefold)[:MAX_LISTED_FOLDERS]


def _ordered_unique(*groups):
    seen, ordered = set(), []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
    return ordered


class Sorter:
    """The app's actions. Each takes the decoded JSON body and returns a dict."""

    def __init__(self, server=None):
        self.server = server
        self.lock = threading.Lock()
        self.scans = {}

    def places(self, body):
        home = Path.home()
        places = [
            {"name": name, "path": str(home / name)}
            for name in ("Downloads", "Desktop", "Documents", "Pictures")
            if (home / name).is_dir()
        ]
        places.append({"name": "Home", "path": str(home)})
        start = home / "Downloads" if (home / "Downloads").is_dir() else home
        return {"places": places, "start": str(start)}

    def browse(self, body):
        folder = _folder_arg(body, "path")
        parent = folder.parent if folder.parent != folder else None
        return {
            "path": str(folder),
            "parent": str(parent) if parent else None,
            "dirs": [{"name": n, "path": str(folder / n)} for n in _subfolders(folder)],
        }

    def scan(self, body):
        folder = _folder_arg(body)
        by = body.get("by", "smart")
        dest = body.get("dest")
        if dest is not None and not isinstance(dest, str):
            raise engine.SortError("invalid destination")
        result = engine.scan(
            folder,
            dest.strip() if dest and dest.strip() else None,
            by=by,
            recursive=bool(body.get("recursive")),
            include_hidden=bool(body.get("includeHidden")),
            duplicates=bool(body.get("duplicates")),
        )
        plan = engine.plan_moves(
            result.source,
            result.dest,
            [(c.path, c.folder, c.reason) for c in result.candidates],
            result.skipped,
        )
        items, files = [], []
        for move in plan.moves:
            try:
                stat = move.source.stat()
            except OSError:
                continue
            items.append((move.source, move.reason))
            name = move.source.name
            files.append({
                "id": len(items) - 1,
                "name": name,
                "path": move.source.relative_to(result.source).as_posix(),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "folder": move.target.parent.relative_to(result.dest).as_posix(),
                "reason": move.reason,
                "kind": engine.category_for(name),
                "ext": Path(name).suffix.lstrip(".").upper()[:4],
            })

        if by == "smart":
            standard = list(engine.SMART_FOLDERS)
        elif by == "type":
            standard = list(engine.CATEGORIES) + [engine.OTHER]
        else:
            standard = sorted({f["folder"] for f in files}, key=str.casefold)
        existing = _subfolders(result.dest) if result.dest.is_dir() else []
        folders = _ordered_unique(standard, [f["folder"] for f in files], existing)

        scan_id = secrets.token_hex(8)
        with self.lock:
            self.scans[scan_id] = {
                "source": result.source,
                "dest": result.dest,
                "items": items,
                "skipped": result.skipped,
            }
            while len(self.scans) > MAX_SCANS_KEPT:
                self.scans.pop(next(iter(self.scans)))
        return {
            "scanId": scan_id,
            "source": str(result.source),
            "dest": str(result.dest),
            "by": by,
            "files": files,
            "folders": folders,
            "alreadySorted": plan.already_sorted,
            "skipped": dict(result.skipped),
        }

    def sort(self, body):
        scan_id = body.get("scanId")
        with self.lock:
            state = self.scans.get(scan_id) if isinstance(scan_id, str) else None
        if state is None:
            raise engine.SortError("this scan is out of date - press Scan again")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise engine.SortError("no files are set to be sorted")
        picked, seen = [], set()
        for choice in choices:
            index = choice.get("id") if isinstance(choice, dict) else None
            folder = choice.get("folder") if isinstance(choice, dict) else None
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(state["items"])
                or index in seen
                or not isinstance(folder, str)
            ):
                raise engine.SortError("invalid file selection - press Scan again")
            seen.add(index)
            path, reason = state["items"][index]
            picked.append((path, engine.safe_folder(folder), reason))

        with self.lock:  # a scan can be used once, even if Sort is double-clicked
            if self.scans.pop(scan_id, None) is None:
                raise engine.SortError("this scan was already used - press Scan again")
        plan = engine.plan_moves(state["source"], state["dest"], picked, state["skipped"])
        copy = bool(body.get("copy"))
        done, errors, log_path = engine.apply_plan(plan, copy=copy)
        counts = Counter(m.target.parent.relative_to(state["dest"]).as_posix() for m in done)
        return {
            "done": len(done),
            "copied": copy,
            "dest": str(state["dest"]),
            "folders": [{"name": n, "count": c} for n, c in counts.most_common()],
            "errors": [
                {"name": m.source.name, "error": exc.strerror or str(exc)} for m, exc in errors
            ],
            "log": str(log_path) if log_path else None,
        }

    def history(self, body):
        folder = _folder_arg(body)
        entries = []
        logs = sorted(folder.glob(engine.LOG_PREFIX + "*.json"), reverse=True)[:50]
        for log in logs:
            try:
                data = engine.load_log(log)
            except engine.SortError:
                continue
            restores, _ = engine.plan_undo(data)
            entries.append({
                "log": str(log),
                "created": data.get("created"),
                "files": len(data["moves"]),
                "restorable": len(restores),
            })
        return {"folder": str(folder), "entries": entries}

    def undo(self, body):
        log = body.get("log")
        if not isinstance(log, str):
            raise engine.SortError("no undo log given")
        path = Path(log)
        if not path.name.startswith(engine.LOG_PREFIX) or path.suffix != ".json":
            raise engine.SortError("that is not an undo log")
        data = engine.load_log(path)
        restores, problems = engine.plan_undo(data)
        done, errors = engine.apply_undo(data, restores)
        return {
            "restored": len(done),
            "problems": [{"name": p.name, "reason": r} for p, r in problems],
            "errors": [
                {"name": m.source.name, "error": exc.strerror or str(exc)} for m, exc in errors
            ],
        }

    def reveal(self, body):
        folder = str(_folder_arg(body, "path"))
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # noqa: S606 - opens Explorer on a folder we validated
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(
                    ["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
        except OSError as exc:
            raise engine.SortError(f"could not open the folder: {exc}") from exc
        return {"ok": True}

    def quit(self, body):
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "FileSorter"
    sys_version = ""

    def _allowed_origins(self):
        port = self.server.server_address[1]
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def _host_ok(self):
        # Blocks DNS-rebinding: a page on another site can resolve its own
        # name to 127.0.0.1, but its requests still carry that other Host.
        return self.headers.get("Host", "") in self._allowed_origins()

    def _send(self, status, payload, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _token_ok(self, presented):
        expected = self.server.token.encode("ascii")
        return secrets.compare_digest(presented.encode("utf-8", "replace"), expected)

    def do_GET(self):  # noqa: N802 - http.server naming
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        url = urlsplit(self.path)
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if url.path != "/":
            return self._json(404, {"error": "not found"})
        key = parse_qs(url.query).get("key", [""])[0]
        if not self._token_ok(key):
            message = (
                "<!doctype html><meta charset=utf-8><title>File Sorter</title>"
                "<p style='font:16px system-ui;margin:40px'>Open File Sorter with the full "
                "link printed in the terminal where you started it.</p>"
            )
            return self._send(403, message.encode("utf-8"), "text/html; charset=utf-8")
        nonce = secrets.token_urlsafe(16)
        page = PAGE.replace("__NONCE__", nonce).replace(
            '"__TOKEN__"', json.dumps(self.server.token)
        )
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        )
        self._send(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
            {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"},
        )

    def do_POST(self):  # noqa: N802 - http.server naming
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        origin = self.headers.get("Origin")
        if origin and origin not in tuple("http://" + o for o in self._allowed_origins()):
            return self._json(403, {"error": "forbidden"})
        if not self._token_ok(self.headers.get("X-Sorter-Token", "")):
            return self._json(403, {"error": "forbidden"})
        name = self.path[len("/api/"):] if self.path.startswith("/api/") else ""
        if name not in ACTIONS:
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad request"})
        if length > MAX_BODY_BYTES:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad request"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "bad request"})
        try:
            data = getattr(self.server.app, name)(body)
        except engine.SortError as exc:
            message = str(exc)
            return self._json(400, {"error": message[:1].upper() + message[1:]})
        except Exception:  # noqa: BLE001 - report, keep the app alive
            traceback.print_exc()
            return self._json(
                500, {"error": "Something went wrong - details are in the terminal."}
            )
        self._json(200, data)

    def log_message(self, format, *args):  # noqa: A002 - quiet terminal
        pass


def create_server(port=DEFAULT_PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.token = secrets.token_urlsafe(32)
    server.app = Sorter(server)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Open the offline File Sorter in your web browser."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"port on 127.0.0.1 to use (default {DEFAULT_PORT}; 0 = any free port)",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="don't open a browser tab automatically"
    )
    args = parser.parse_args(argv)
    try:
        server = create_server(args.port)
    except OSError:
        if args.port == 0:
            raise
        server = create_server(0)  # the usual port is busy; take any free one
    url = f"http://127.0.0.1:{server.server_address[1]}/?key={server.token}"
    print("File Sorter is running. If no browser tab opened, visit:")
    print(f"  {url}")
    print("It only listens on this computer. Press Ctrl+C here (or Quit in the page) to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    print("File Sorter stopped.")
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Sorter</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234c6ef5'/%3E%3Cpath d='M9 11h14M9 16h10M9 21h6' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<style nonce="__NONCE__">
:root {
  --bg: #f4f5f8; --surface: #ffffff; --surface-2: #f0f2f6; --border: #e1e4ea; --border-strong: #cdd2db;
  --text: #15171c; --muted: #5d6471; --faint: #8a919d;
  --accent: #4263eb; --accent-hover: #3451d1; --accent-soft: #edf1ff; --accent-ink: #ffffff;
  --ok: #2b8a3e; --ok-soft: #e6f6ea; --danger: #c92a2a; --danger-soft: #fdeded;
  --shadow: 0 1px 2px rgba(16,24,40,.05), 0 2px 8px rgba(16,24,40,.06);
  --radius: 14px; --radius-sm: 9px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --surface: #171a20; --surface-2: #1f232b; --border: #2a2f38; --border-strong: #3b414c;
    --text: #e9ecf1; --muted: #a3aab6; --faint: #78808c;
    --accent: #7c93ff; --accent-hover: #95a8ff; --accent-soft: #1e2645; --accent-ink: #0f1115;
    --ok: #51cf66; --ok-soft: #14301c; --danger: #ff8787; --danger-soft: #3a1c1c;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 2px 10px rgba(0,0,0,.3);
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; -webkit-font-smoothing: antialiased; padding-bottom: 100px; }
[hidden] { display: none !important; }
button, input, select { font: inherit; color: inherit; }
h1, h2, h3, p { margin: 0; }

.topbar { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 28px; background: color-mix(in srgb, var(--bg) 86%, transparent); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border); }
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.logo { width: 36px; height: 36px; border-radius: 10px; flex: none; display: grid; place-items: center; background: linear-gradient(135deg, #4c6ef5, #845ef7); box-shadow: var(--shadow); }
h1 { font-size: 16px; letter-spacing: -.01em; }
.tagline { color: var(--muted); font-size: 12.5px; display: flex; align-items: center; gap: 6px; }
.tagline::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ok); flex: none; }
.top-actions { display: flex; gap: 4px; }

main { max-width: 1180px; margin: 0 auto; padding: 24px 28px; display: flex; flex-direction: column; gap: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
.setup { padding: 22px 24px; display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 8px; }
.label { font-weight: 650; font-size: 13px; }
.hint { color: var(--muted); font-size: 12.5px; }
.pathrow { display: flex; gap: 8px; }
.input { flex: 1; min-width: 0; height: 40px; padding: 0 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface); outline: none; transition: border-color .15s, box-shadow .15s; }
.input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }

.btn { height: 40px; padding: 0 16px; border-radius: var(--radius-sm); border: 1px solid var(--border-strong); background: var(--surface); cursor: pointer; font-weight: 600; white-space: nowrap; display: inline-flex; align-items: center; justify-content: center; gap: 8px; transition: background .15s, border-color .15s, transform .05s; }
.btn:hover { background: var(--surface-2); }
.btn:active { transform: translateY(1px); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.btn.ghost { border-color: transparent; background: transparent; color: var(--muted); }
.btn.ghost:hover { background: var(--surface-2); color: var(--text); }
.btn.big { height: 44px; padding: 0 22px; font-size: 15px; }
.btn[disabled] { opacity: .5; cursor: default; transform: none; }
:is(.btn, .chip, .mode, .side-item, .dir, .select, summary):focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { border: 1px solid var(--border); background: var(--surface-2); border-radius: 999px; padding: 3px 12px; font-size: 12.5px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
.chip:hover { border-color: var(--border-strong); }
.chip.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.chip.static { cursor: default; background: var(--surface); }

.modes { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.mode { text-align: left; border: 1.5px solid var(--border); border-radius: 12px; padding: 12px 14px; background: var(--surface); cursor: pointer; display: flex; flex-direction: column; gap: 4px; transition: border-color .15s, background .15s; }
.mode:hover { border-color: var(--border-strong); }
.mode[aria-checked="true"] { border-color: var(--accent); background: var(--accent-soft); }
.mode-title { font-weight: 650; display: flex; align-items: center; gap: 8px; }
.mode-desc { font-size: 12.5px; color: var(--muted); }
.rec { font-size: 10px; font-weight: 750; letter-spacing: .05em; text-transform: uppercase; color: var(--accent); border: 1px solid var(--accent); border-radius: 999px; padding: 0 7px; line-height: 16px; }

.options { display: flex; flex-wrap: wrap; gap: 8px 24px; }
.check { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.check input { width: 16px; height: 16px; accent-color: var(--accent); margin: 0; flex: none; }
.more summary { cursor: pointer; color: var(--muted); font-size: 13px; width: fit-content; }
.more[open] summary { margin-bottom: 8px; }
.safety { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; border-top: 1px solid var(--border); padding-top: 12px; }

.results-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
h2 { font-size: 20px; letter-spacing: -.01em; }
.muted { color: var(--muted); }
.search { max-width: 280px; flex: 0 1 280px; }
.results-body { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 16px; align-items: start; }
.sidebar { padding: 8px; position: sticky; top: 76px; max-height: calc(100vh - 190px); overflow: auto; }
.side-item { width: 100%; display: flex; align-items: center; gap: 10px; padding: 7px 8px; border-radius: 9px; border: 0; background: transparent; cursor: pointer; text-align: left; }
.side-item:hover { background: var(--surface-2); }
.side-item.active { background: var(--accent-soft); }
.side-item .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.count { margin-left: auto; font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; background: var(--surface-2); border-radius: 999px; padding: 0 8px; }
.side-item.active .count { background: var(--surface); }
.side-sep { height: 1px; background: var(--border); margin: 6px 8px; }
.ico { width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; font-size: 15px; flex: none; background: color-mix(in srgb, var(--c) 18%, transparent); }

.list { overflow: hidden; }
.list-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.list-title { font-weight: 650; }
.bulk { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.select { height: 34px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); padding: 0 8px; cursor: pointer; max-width: 220px; }
.row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto 244px; align-items: center; gap: 14px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
.row:last-child { border-bottom: 0; }
.row:hover { background: color-mix(in srgb, var(--surface-2) 55%, transparent); }
.ext { width: 40px; height: 40px; border-radius: 9px; display: grid; place-items: center; font-size: 10px; font-weight: 800; letter-spacing: .02em; color: var(--c); background: color-mix(in srgb, var(--c) 15%, transparent); }
.fname { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fsub { font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.why.smart::before { content: "\2726  "; color: var(--accent); }
.why.mine { color: var(--accent); }
.meta { font-size: 12px; color: var(--faint); text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; line-height: 1.4; }
.dest { position: relative; display: flex; }
.dest::before { content: ""; position: absolute; left: 11px; top: 50%; width: 8px; height: 8px; margin-top: -4px; border-radius: 50%; background: var(--c); pointer-events: none; }
.dest .select { width: 100%; max-width: none; padding-left: 26px; font-size: 13px; }
.row.leave .fname, .row.leave .ext { opacity: .55; }
.rows-empty { padding: 40px; text-align: center; color: var(--muted); }

.actionbar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 6; background: color-mix(in srgb, var(--surface) 90%, transparent); backdrop-filter: blur(12px); border-top: 1px solid var(--border); }
.actionbar-inner { max-width: 1180px; margin: 0 auto; padding: 12px 28px; display: flex; align-items: center; gap: 20px; }
.summary { margin-right: auto; }
.summary .sub { color: var(--muted); font-size: 12.5px; }

.done, .empty { padding: 40px 28px; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 10px; }
.big-ico { width: 64px; height: 64px; border-radius: 50%; display: grid; place-items: center; font-size: 28px; background: var(--ok-soft); color: var(--ok); }
.big-ico.neutral { background: var(--surface-2); color: var(--muted); }
.done h2, .empty h2 { font-size: 24px; }
.done .chips { justify-content: center; max-width: 760px; margin: 6px 0 10px; }
.btnrow { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }
.errors { text-align: left; background: var(--danger-soft); color: var(--danger); border-radius: 10px; padding: 10px 14px; font-size: 13px; max-width: 760px; width: 100%; }

dialog { border: 1px solid var(--border); border-radius: 16px; padding: 0; background: var(--surface); color: var(--text); box-shadow: 0 24px 64px rgba(0,0,0,.28); width: min(580px, calc(100vw - 32px)); }
dialog::backdrop { background: rgba(15,17,21,.45); backdrop-filter: blur(2px); }
.dlg-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
.dlg-head h3 { font-size: 16px; }
.dlg-body { padding: 12px 18px; max-height: 58vh; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.dlg-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--border); }
.crumb { display: flex; align-items: center; gap: 8px; padding: 10px 18px 0; }
.crumb .path { flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dlg-places { padding: 10px 18px 0; }
.dir { display: flex; align-items: center; gap: 10px; width: 100%; padding: 8px 10px; border: 0; background: transparent; border-radius: 8px; cursor: pointer; text-align: left; }
.dir:hover { background: var(--surface-2); }
.grow { flex: 1; min-width: 0; }
.hist { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
.hist:last-child { border-bottom: 0; }

.toast { position: fixed; left: 50%; bottom: 96px; transform: translate(-50%, 16px); opacity: 0; pointer-events: none; background: var(--text); color: var(--bg); padding: 10px 16px; border-radius: 10px; box-shadow: var(--shadow); transition: opacity .2s, transform .2s; z-index: 20; max-width: min(560px, calc(100vw - 32px)); }
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.error { background: var(--danger); color: #fff; }
.spinner { width: 14px; height: 14px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 860px) {
  .topbar, main, .actionbar-inner { padding-left: 16px; padding-right: 16px; }
  .tagline { display: none; }
  .modes { grid-template-columns: repeat(2, 1fr); }
  .pathrow { flex-wrap: wrap; }
  .pathrow .input { flex-basis: 100%; }
  .pathrow .btn { flex: 1; }
  .results-head { flex-direction: column; align-items: stretch; }
  .search { max-width: none; flex: auto; }
  .results-body { grid-template-columns: minmax(0, 1fr); }
  .sidebar { position: static; display: flex; overflow-x: auto; gap: 4px; max-height: none; }
  .side-item { width: auto; flex: none; }
  .side-sep { display: none; }
  .list-head { flex-wrap: wrap; }
  .row { grid-template-columns: auto minmax(0, 1fr); }
  .row .meta { display: none; }
  .row .dest { grid-column: 1 / -1; }
  .actionbar-inner { flex-wrap: wrap; gap: 10px; }
  .actionbar .btn.big { flex: 1; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo" aria-hidden="true">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M3 5h12M3 9h8M3 13h4" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>
    </div>
    <div>
      <h1>File Sorter</h1>
      <p class="tagline">Works offline &middot; your files never leave this computer</p>
    </div>
  </div>
  <div class="top-actions">
    <button id="historyBtn" class="btn ghost" type="button">History</button>
    <button id="quitBtn" class="btn ghost" type="button">Quit</button>
  </div>
</header>

<main>
  <section class="card setup" aria-labelledby="folderLabel">
    <div class="field">
      <label class="label" id="folderLabel" for="folderInput">Folder to tidy</label>
      <div class="pathrow">
        <input id="folderInput" class="input mono" spellcheck="false" autocomplete="off" placeholder="Paste or browse to a folder">
        <button id="browseBtn" class="btn" type="button">Browse&hellip;</button>
        <button id="scanBtn" class="btn primary" type="button">Scan folder</button>
      </div>
      <div class="chips" id="places"></div>
    </div>
    <div class="field">
      <div class="label" id="modeLabel">How to sort</div>
      <div class="modes" id="modes" role="radiogroup" aria-labelledby="modeLabel"></div>
    </div>
    <div class="options">
      <label class="check"><input type="checkbox" id="optDuplicates"><span>Put duplicate copies in a <b>Duplicates</b> folder</span></label>
      <label class="check"><input type="checkbox" id="optRecursive"><span>Include files inside subfolders</span></label>
      <label class="check"><input type="checkbox" id="optHidden"><span>Include hidden files</span></label>
    </div>
    <details class="more" id="moreBox">
      <summary>Put the sorted folders somewhere else</summary>
      <div class="pathrow">
        <input id="destInput" class="input mono" spellcheck="false" autocomplete="off" placeholder="Same as the folder above">
        <button id="destBrowseBtn" class="btn" type="button">Browse&hellip;</button>
      </div>
    </details>
    <p class="safety">&#128274; Nothing moves until you press Sort. Existing files are never overwritten, and every sort can be undone.</p>
  </section>

  <section id="results" hidden>
    <div class="results-head">
      <div>
        <h2 id="resultsTitle"></h2>
        <p class="muted" id="resultsSub"></p>
      </div>
      <input id="search" class="input search" type="search" placeholder="Filter files&hellip;" aria-label="Filter files">
    </div>
    <div class="results-body">
      <nav class="sidebar card" id="sidebar" aria-label="Folders"></nav>
      <div class="card list">
        <div class="list-head">
          <span class="list-title" id="listTitle">All files</span>
          <label class="bulk"><span class="muted">Move all shown to</span><span id="bulkSlot"></span></label>
        </div>
        <div id="rows" role="list"></div>
      </div>
    </div>
  </section>

  <section id="done" class="card done" hidden aria-live="polite"></section>
  <section id="empty" class="card empty" hidden></section>
</main>

<footer class="actionbar" id="actionbar" hidden>
  <div class="actionbar-inner">
    <div class="summary" id="summary"></div>
    <label class="check"><input type="checkbox" id="optCopy"><span>Copy instead of move</span></label>
    <button id="sortBtn" class="btn primary big" type="button">Sort files</button>
  </div>
</footer>

<dialog id="browserDlg" aria-labelledby="browserTitle">
  <div class="dlg-head"><h3 id="browserTitle">Choose a folder</h3><button class="btn ghost" type="button" data-close>Close</button></div>
  <div class="chips dlg-places" id="browserPlaces"></div>
  <div class="crumb"><button id="upBtn" class="btn" type="button">&uarr; Up</button><span class="path" id="crumbPath"></span></div>
  <div class="dlg-body" id="dirList"></div>
  <div class="dlg-foot"><button class="btn" type="button" data-close>Cancel</button><button id="chooseBtn" class="btn primary" type="button">Use this folder</button></div>
</dialog>

<dialog id="historyDlg" aria-labelledby="historyTitle">
  <div class="dlg-head"><div><h3 id="historyTitle">Sort history</h3><p class="hint" id="historyFolder"></p></div><button class="btn ghost" type="button" data-close>Close</button></div>
  <div class="dlg-body" id="historyBody"></div>
</dialog>

<div id="toast" class="toast" role="status" aria-live="polite"></div>

<script nonce="__NONCE__">
"use strict";
const TOKEN = "__TOKEN__";
const LEAVE = "\u0001leave";
const NEW_FOLDER = "\u0001new";
const META = {
  "Job Applications": ["\u{1F4BC}", "#4c6ef5"],
  "Finance": ["\u{1F4B0}", "#37b24d"],
  "Contracts & Legal": ["\u{1F4DC}", "#ae3ec9"],
  "Travel & Tickets": ["✈️", "#1c7ed6"],
  "Certificates & Diplomas": ["\u{1F393}", "#f59f00"],
  "Manuals & Guides": ["\u{1F4D8}", "#1098ad"],
  "Documents": ["\u{1F4C4}", "#748ffc"],
  "Spreadsheets": ["\u{1F4CA}", "#40c057"],
  "Presentations": ["\u{1F4FD}️", "#f76707"],
  "Photos": ["\u{1F4F7}", "#e64980"],
  "Screenshots": ["\u{1F5BC}️", "#7950f2"],
  "Images": ["\u{1F3A8}", "#f06595"],
  "Audio": ["\u{1F3B5}", "#9775fa"],
  "Video": ["\u{1F3AC}", "#fd7e14"],
  "Archives": ["\u{1F5DC}️", "#868e96"],
  "Installers": ["\u{1F4E6}", "#74b816"],
  "Code": ["\u{1F4BB}", "#339af0"],
  "Fonts": ["\u{1F524}", "#868e96"],
  "Duplicates": ["\u{1F46F}", "#fa5252"],
  "Other": ["\u{1F4C1}", "#868e96"],
};
const PALETTE = ["#4c6ef5", "#37b24d", "#ae3ec9", "#1c7ed6", "#f59f00", "#1098ad", "#e64980", "#7950f2", "#fd7e14", "#74b816"];
const MODES = [
  {id: "smart", title: "Smart", rec: true, desc: "By topic, then kind: invoices, CVs, contracts, tickets, photos, screenshots…"},
  {id: "type", title: "File type", desc: "Documents, Images, Audio, Archives, Installers…"},
  {id: "date", title: "Month", desc: "2024 › 2024-03, by when each file last changed"},
  {id: "extension", title: "Extension", desc: "One folder per extension: PDF, DOCX, ZIP…"},
];
const SMART_REASON = /mentions|named like|same content/;
const S = {by: "smart", start: "", places: [], scan: null, choice: new Map(), custom: [], filter: "*", query: "", browseTarget: null, browsePath: null};
const $ = (id) => document.getElementById(id);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "vars") for (const [name, value] of Object.entries(v)) el.style.setProperty(name, value);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(name, body) {
  let res;
  try {
    res = await fetch("/api/" + name, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Sorter-Token": TOKEN},
      body: JSON.stringify(body || {}),
    });
  } catch (err) {
    throw new Error("Lost contact with File Sorter. Is it still running in the terminal?");
  }
  let data = {};
  try { data = await res.json(); } catch (err) { /* keep {} */ }
  if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ")");
  return data;
}

let toastTimer = 0;
function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast" + (isError ? " error" : ""); }, isError ? 6000 : 3500);
}

const store = {
  get() { try { return JSON.parse(localStorage.getItem("fileSorter") || "{}"); } catch (err) { return {}; } },
  set(value) { try { localStorage.setItem("fileSorter", JSON.stringify(value)); } catch (err) { /* private mode */ } },
};
function saveSettings() {
  store.set({
    folder: $("folderInput").value.trim(), dest: $("destInput").value.trim(), by: S.by,
    duplicates: $("optDuplicates").checked, recursive: $("optRecursive").checked,
    hidden: $("optHidden").checked, copy: $("optCopy").checked,
  });
}

function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
function baseName(path) { return String(path).split(/[\\/]/).filter(Boolean).pop() || path; }
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes, i = -1;
  do { value /= 1024; i++; } while (value >= 1024 && i < units.length - 1);
  return (value < 10 ? value.toFixed(1) : Math.round(value)) + " " + units[i];
}
function fmtDate(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"});
}
function fmtWhen(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
}
function hashColor(text) {
  let n = 0;
  for (const ch of text) n = (n * 31 + ch.codePointAt(0)) >>> 0;
  return PALETTE[n % PALETTE.length];
}
function folderMeta(folder) {
  if (folder === LEAVE) return {icon: "⏸️", color: "#adb5bd", label: "Leave where it is"};
  const known = META[folder];
  const label = folder.split("/").join(" › ");
  return known ? {icon: known[0], color: known[1], label} : {icon: "\u{1F4C1}", color: hashColor(folder), label};
}
function kindColor(kind) { return (META[kind] || META.Other)[1]; }

function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.disabled = true;
    button.replaceChildren(h("span", {class: "spinner", "aria-hidden": "true"}), label);
  } else {
    button.disabled = false;
    if (button.dataset.label) button.textContent = button.dataset.label;
  }
}

/* ---------- setup ---------- */

function renderPlaces() {
  const current = $("folderInput").value.trim();
  $("places").replaceChildren(...S.places.map((p) =>
    h("button", {type: "button", class: "chip" + (p.path === current ? " active" : ""), title: p.path,
      onclick: () => { $("folderInput").value = p.path; staleCheck(); scan(); }}, p.name)));
}

function renderModes() {
  $("modes").replaceChildren(...MODES.map((m) =>
    h("button", {type: "button", class: "mode", role: "radio", "aria-checked": String(S.by === m.id),
      onclick: () => { if (S.by === m.id) return; S.by = m.id; renderModes(); saveSettings(); if (S.scan) scan(); }},
      h("span", {class: "mode-title"}, m.title, m.rec ? h("span", {class: "rec"}, "Recommended") : null),
      h("span", {class: "mode-desc"}, m.desc))));
}

function hideResults() {
  S.scan = null;
  for (const id of ["results", "actionbar", "done", "empty"]) $(id).hidden = true;
}

function staleCheck() {
  renderPlaces();
  if (S.scan && $("folderInput").value.trim() !== S.scan.source) hideResults();
}

async function scan() {
  const folder = $("folderInput").value.trim();
  if (!folder) { toast("Choose a folder to tidy first.", true); $("folderInput").focus(); return; }
  setBusy($("scanBtn"), true, "Scanning…");
  try {
    const data = await api("scan", {
      folder, by: S.by, dest: $("destInput").value.trim(),
      recursive: $("optRecursive").checked, includeHidden: $("optHidden").checked,
      duplicates: $("optDuplicates").checked,
    });
    S.scan = data;
    S.choice = new Map(data.files.map((f) => [f.id, f.folder]));
    S.custom = [];
    S.filter = "*";
    S.query = "";
    $("search").value = "";
    $("folderInput").value = data.source;
    saveSettings();
    renderPlaces();
    showResults();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy($("scanBtn"), false);
  }
}

function describeSkipped(skipped) {
  const parts = Object.entries(skipped || {}).map(([why, n]) => n + " " + why);
  return parts.length ? "skipped " + parts.join(", ") : "";
}

function showResults() {
  const data = S.scan;
  $("done").hidden = true;
  if (!data.files.length) {
    $("results").hidden = true;
    $("actionbar").hidden = true;
    const extra = [data.alreadySorted ? plural(data.alreadySorted, "file") + " already in place" : "", describeSkipped(data.skipped)].filter(Boolean).join(" · ");
    $("empty").replaceChildren(
      h("div", {class: "big-ico neutral"}, "✨"),
      h("h2", {}, "Nothing to sort here"),
      h("p", {class: "muted"}, baseName(data.source) + " is already tidy." + (extra ? " (" + extra + ")" : "")));
    $("empty").hidden = false;
    return;
  }
  $("empty").hidden = true;
  $("results").hidden = false;
  $("actionbar").hidden = false;
  $("optCopy").checked = Boolean(store.get().copy);
  render();
}

/* ---------- results ---------- */

function folderList() {
  const used = [...new Set(S.choice.values())].filter((f) => f !== LEAVE);
  const all = [...S.scan.folders, ...S.custom, ...used];
  return [...new Set(all)];
}

function buildSelect(placeholder) {
  const select = h("select", {class: "select"});
  if (placeholder) select.append(h("option", {value: ""}, placeholder));
  const group = h("optgroup", {label: "Folders"});
  for (const folder of folderList()) {
    const m = folderMeta(folder);
    group.append(h("option", {value: folder}, m.icon + "  " + m.label));
  }
  select.append(group, h("optgroup", {label: "More"},
    h("option", {value: LEAVE}, "⏸️  Leave where it is"),
    h("option", {value: NEW_FOLDER}, "➕  New folder…")));
  return select;
}

const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
function byName(a, b) { return collator.compare(a.path, b.path); }

function visibleFiles() {
  const q = S.query.toLowerCase();
  return S.scan.files.slice().sort(byName).filter((f) =>
    (S.filter === "*" || S.choice.get(f.id) === S.filter) &&
    (!q || f.path.toLowerCase().includes(q) || f.reason.toLowerCase().includes(q)));
}

function render() { renderHead(); renderSidebar(); renderRows(); renderSummary(); }

function renderHead() {
  const d = S.scan;
  const folders = new Set(d.files.map((f) => f.folder)).size;
  $("resultsTitle").textContent = plural(d.files.length, "file") + " ready to sort";
  const bits = [
    "Suggested " + plural(folders, "folder") + " inside " + baseName(d.dest),
    d.alreadySorted ? d.alreadySorted + " already in place" : "",
    describeSkipped(d.skipped),
  ].filter(Boolean);
  $("resultsSub").textContent = bits.join(" · ");
  $("resultsSub").title = d.dest;
}

function sideItem(key, label, icon, color, count) {
  return h("button", {type: "button", class: "side-item" + (S.filter === key ? " active" : ""),
    onclick: () => { S.filter = key; renderSidebar(); renderRows(); }},
    h("span", {class: "ico", vars: {"--c": color}}, icon),
    h("span", {class: "name"}, label),
    h("span", {class: "count"}, String(count)));
}

function renderSidebar() {
  const counts = new Map();
  for (const folder of S.choice.values()) counts.set(folder, (counts.get(folder) || 0) + 1);
  const items = [sideItem("*", "All files", "\u{1F5C2}️", "#4c6ef5", S.choice.size), h("div", {class: "side-sep"})];
  for (const folder of folderList()) {
    if (!counts.get(folder) && S.filter !== folder) continue;
    const m = folderMeta(folder);
    items.push(sideItem(folder, m.label, m.icon, m.color, counts.get(folder) || 0));
  }
  if (counts.get(LEAVE) || S.filter === LEAVE) {
    const m = folderMeta(LEAVE);
    items.push(h("div", {class: "side-sep"}), sideItem(LEAVE, m.label, m.icon, m.color, counts.get(LEAVE) || 0));
  }
  $("sidebar").replaceChildren(...items);
}

function reasonFor(file) {
  const chosen = S.choice.get(file.id);
  if (chosen !== file.folder) {
    return h("span", {class: "why mine"}, "Your choice · suggested " + folderMeta(file.folder).label);
  }
  return h("span", {class: "why" + (SMART_REASON.test(file.reason) ? " smart" : "")}, file.reason);
}

function renderRows() {
  const files = visibleFiles();
  $("listTitle").textContent = (S.filter === "*" ? "All files" : folderMeta(S.filter).label) + " · " + files.length;
  const bulk = buildSelect("Choose…");
  bulk.setAttribute("aria-label", "Move all shown files to");
  bulk.disabled = !files.length;
  bulk.addEventListener("change", () => {
    const value = bulk.value;
    if (value) choose(visibleFiles().map((f) => f.id), value);
  });
  $("bulkSlot").replaceChildren(bulk);

  if (!files.length) {
    $("rows").replaceChildren(h("div", {class: "rows-empty"}, S.query ? "No files match “" + S.query + "”." : "No files here any more."));
    return;
  }
  const template = buildSelect();
  const fragment = document.createDocumentFragment();
  for (const file of files) {
    const folder = S.choice.get(file.id);
    const select = template.cloneNode(true);
    select.value = folder;
    select.setAttribute("aria-label", "Where to put " + file.name);
    select.addEventListener("change", () => choose([file.id], select.value, select));
    const where = file.path.includes("/") ? "in " + file.path.slice(0, file.path.lastIndexOf("/")) + " · " : "";
    fragment.append(h("div", {class: "row" + (folder === LEAVE ? " leave" : ""), role: "listitem"},
      h("div", {class: "ext", vars: {"--c": kindColor(file.kind)}}, file.ext || "FILE"),
      h("div", {},
        h("div", {class: "fname", title: file.path}, file.name),
        h("div", {class: "fsub"}, where, reasonFor(file))),
      h("div", {class: "meta"}, fmtSize(file.size), h("br"), fmtDate(file.modified)),
      h("div", {class: "dest", vars: {"--c": folderMeta(folder).color}}, select)));
  }
  $("rows").replaceChildren(fragment);
}

function cleanFolderName(raw) {
  const parts = raw.split("/").map((p) => p.trim());
  const bad = parts.some((p) => !p || p === "." || p === ".." || /[<>:"|?*\\\u0000-\u001f]/.test(p) || /[. ]$/.test(p));
  return bad || parts.length > 4 ? null : parts.join("/");
}

function choose(ids, value, select) {
  if (value === NEW_FOLDER) {
    const name = cleanFolderName(window.prompt("Name for the new folder:") || "");
    if (!name) {
      if (select) select.value = S.choice.get(ids[0]);
      renderRows();
      return;
    }
    if (!folderList().includes(name)) S.custom.push(name);
    value = name;
  }
  for (const id of ids) S.choice.set(id, value);
  render();
}

function renderSummary() {
  const moving = [...S.choice.values()].filter((f) => f !== LEAVE);
  const folders = new Set(moving).size;
  const left = S.choice.size - moving.length;
  const copy = $("optCopy").checked;
  $("summary").replaceChildren(
    h("div", {}, h("strong", {}, plural(moving.length, "file")), " → " + plural(folders, "folder")),
    h("div", {class: "sub"}, left ? left + " left where they are" : "Every file has a place"));
  $("sortBtn").textContent = moving.length ? (copy ? "Copy " : "Sort ") + plural(moving.length, "file") : "Nothing to sort";
  $("sortBtn").disabled = !moving.length;
}

async function doSort() {
  const choices = [];
  for (const [id, folder] of S.choice) if (folder !== LEAVE) choices.push({id, folder});
  if (!choices.length) return;
  const copy = $("optCopy").checked;
  setBusy($("sortBtn"), true, copy ? "Copying…" : "Sorting…");
  try {
    const result = await api("sort", {scanId: S.scan.scanId, choices, copy});
    showDone(result);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy($("sortBtn"), false);
  }
}

/* ---------- after sorting ---------- */

function showDone(result) {
  hideResults();
  const box = $("done");
  const verb = result.copied ? "Copied" : "Sorted";
  const kids = [
    h("div", {class: "big-ico"}, "✓"),
    h("h2", {}, verb + " " + plural(result.done, "file")),
    h("p", {class: "muted", title: result.dest}, "into " + plural(result.folders.length, "folder") + " in " + baseName(result.dest)),
    h("div", {class: "chips"}, result.folders.map((f) => {
      const m = folderMeta(f.name);
      return h("span", {class: "chip static"}, m.icon + " " + m.label, h("span", {class: "count"}, String(f.count)));
    })),
  ];
  if (result.errors.length) {
    kids.push(h("div", {class: "errors"},
      h("strong", {}, plural(result.errors.length, "file") + " could not be moved:"),
      h("ul", {}, result.errors.slice(0, 12).map((e) => h("li", {}, e.name + " — " + e.error)))));
  }
  kids.push(h("div", {class: "btnrow"},
    h("button", {type: "button", class: "btn primary", onclick: () => reveal(result.dest)}, "Open folder"),
    result.log ? h("button", {type: "button", class: "btn", onclick: () => undoFromDone(result.log)}, "↩ Undo this sort") : null,
    h("button", {type: "button", class: "btn ghost", onclick: () => { hideResults(); scan(); }}, "Scan again")));
  box.replaceChildren(...kids);
  box.hidden = false;
  box.scrollIntoView({behavior: "smooth", block: "center"});
}

async function reveal(path) {
  try { await api("reveal", {path}); } catch (err) { toast(err.message, true); }
}

async function undoLog(log) {
  if (!window.confirm("Put every file from this sort back where it was?")) return null;
  try {
    const r = await api("undo", {log});
    const issues = r.problems.length + r.errors.length;
    toast("Put back " + plural(r.restored, "file") + (issues ? " · " + issues + " could not be restored" : ""), issues > 0);
    return r;
  } catch (err) {
    toast(err.message, true);
    return null;
  }
}

async function undoFromDone(log) {
  const r = await undoLog(log);
  if (!r) return;
  $("done").replaceChildren(
    h("div", {class: "big-ico neutral"}, "↩"),
    h("h2", {}, "Undone"),
    h("p", {class: "muted"}, plural(r.restored, "file") + " went back where they were."),
    h("div", {class: "btnrow"}, h("button", {type: "button", class: "btn primary", onclick: () => { hideResults(); scan(); }}, "Scan again")));
}

/* ---------- dialogs ---------- */

async function openBrowser(target) {
  S.browseTarget = target;
  $("browserPlaces").replaceChildren(...S.places.map((p) =>
    h("button", {type: "button", class: "chip", onclick: () => browseTo(p.path)}, p.name)));
  $("browserDlg").showModal();
  const start = target.value.trim() || $("folderInput").value.trim() || S.start;
  if (!(await browseTo(start))) await browseTo(S.start);
}

async function browseTo(path) {
  try {
    const r = await api("browse", {path});
    S.browsePath = r.path;
    $("crumbPath").textContent = r.path;
    $("crumbPath").title = r.path;
    $("upBtn").disabled = !r.parent;
    $("upBtn").onclick = () => browseTo(r.parent);
    const list = r.dirs.length ? r.dirs.map((d) =>
      h("button", {type: "button", class: "dir", onclick: () => browseTo(d.path)},
        h("span", {"aria-hidden": "true"}, "\u{1F4C1}"), h("span", {class: "grow"}, d.name), h("span", {class: "muted", "aria-hidden": "true"}, "›")))
      : [h("p", {class: "muted"}, "No subfolders here. Press “Use this folder” to pick it.")];
    $("dirList").replaceChildren(...list);
    $("dirList").scrollTop = 0;
    return true;
  } catch (err) {
    toast(err.message, true);
    return false;
  }
}

function chooseBrowsed() {
  const target = S.browseTarget;
  $("browserDlg").close();
  if (!target || !S.browsePath) return;
  target.value = S.browsePath;
  saveSettings();
  if (target === $("folderInput")) { staleCheck(); scan(); }
  else if (S.scan) scan();
}

async function openHistory() {
  const folder = $("destInput").value.trim() || $("folderInput").value.trim();
  const body = $("historyBody");
  $("historyFolder").textContent = folder;
  body.replaceChildren(h("p", {class: "muted"}, "Loading…"));
  if (!$("historyDlg").open) $("historyDlg").showModal();
  try {
    const r = await api("history", {folder});
    $("historyFolder").textContent = r.folder;
    if (!r.entries.length) {
      body.replaceChildren(h("p", {class: "muted"}, "No sorts recorded in this folder yet."));
      return;
    }
    body.replaceChildren(...r.entries.map((e) => {
      const status = e.restorable === 0 ? "nothing left to put back"
        : e.restorable < e.files ? e.restorable + " can still go back" : "can be undone";
      return h("div", {class: "hist"},
        h("span", {class: "ico", vars: {"--c": "#4c6ef5"}}, "\u{1F558}"),
        h("div", {class: "grow"},
          h("div", {class: "fname"}, fmtWhen(e.created)),
          h("div", {class: "fsub"}, plural(e.files, "file") + " · " + status)),
        h("button", {type: "button", class: "btn", disabled: e.restorable === 0,
          onclick: async () => { if (await undoLog(e.log)) { hideResults(); openHistory(); } }},
          e.restorable === 0 ? "Undone" : "Undo"));
    }));
  } catch (err) {
    body.replaceChildren(h("p", {class: "muted"}, err.message));
  }
}

async function quitApp() {
  if (!window.confirm("Close File Sorter? You can start it again from the terminal any time.")) return;
  try { await api("quit"); } catch (err) { /* already gone */ }
  document.body.replaceChildren(h("main", {}, h("section", {class: "card empty"},
    h("div", {class: "big-ico neutral"}, "\u{1F44B}"),
    h("h2", {}, "File Sorter has stopped"),
    h("p", {class: "muted"}, "You can close this tab."))));
}

/* ---------- start ---------- */

async function init() {
  const saved = store.get();
  S.by = MODES.some((m) => m.id === saved.by) ? saved.by : "smart";
  $("optDuplicates").checked = saved.duplicates ?? true;
  $("optRecursive").checked = Boolean(saved.recursive);
  $("optHidden").checked = Boolean(saved.hidden);
  $("optCopy").checked = Boolean(saved.copy);
  $("destInput").value = saved.dest || "";
  if (saved.dest) $("moreBox").open = true;
  renderModes();

  $("folderInput").addEventListener("input", staleCheck);
  $("folderInput").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
  $("scanBtn").addEventListener("click", scan);
  $("browseBtn").addEventListener("click", () => openBrowser($("folderInput")));
  $("destBrowseBtn").addEventListener("click", () => openBrowser($("destInput")));
  $("destInput").addEventListener("change", () => { saveSettings(); if (S.scan) scan(); });
  for (const id of ["optDuplicates", "optRecursive", "optHidden"]) {
    $(id).addEventListener("change", () => { saveSettings(); if (S.scan) scan(); });
  }
  $("optCopy").addEventListener("change", () => { saveSettings(); renderSummary(); });
  $("search").addEventListener("input", () => { S.query = $("search").value.trim(); renderRows(); });
  $("sortBtn").addEventListener("click", doSort);
  $("historyBtn").addEventListener("click", openHistory);
  $("quitBtn").addEventListener("click", quitApp);
  $("chooseBtn").addEventListener("click", chooseBrowsed);
  for (const btn of document.querySelectorAll("[data-close]")) {
    btn.addEventListener("click", () => btn.closest("dialog").close());
  }

  try {
    const info = await api("places");
    S.places = info.places;
    S.start = info.start;
    $("folderInput").value = saved.folder || info.start;
  } catch (err) {
    toast(err.message, true);
  }
  renderPlaces();
}

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
