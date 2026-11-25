from collections import Counter, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parent
ENGINE = BASE.parents[1]
LOGDIR = Path(os.environ["LOCALAPPDATA"]) / "VIce" / "launcher"

SERVER_VERSION = "VIce Studio local v4"
TOKEN = secrets.token_urlsafe(24)

PROC_LOCK = threading.Lock()
PROC = None
LIVE_LOCK = threading.Lock()
LIVE_LINES = deque(maxlen=500)
LIVE_STATE = {
    "start": None,
    "exit": None,
    "fps": None,
    "ms": None,
    "draws": None,
    "dropped": None,
    "counts": {},
}

DISK_CACHE_LOCK = threading.Lock()
DISK_CACHE = {}

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FPS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s+fps,\s+(\d+(?:\.\d+)?)\s+ms/frame.*?(\d+)\s+draws/frame(?:,\s+dropped\s+(\d+))?"
)
LOG_CATEGORIES = (
    ("host pipeline refused", "Pipeline rejected"),
    ("host mesh pipeline refused", "Mesh pipeline rejected"),
    ("host root signature refused", "Root signature rejected"),
    ("slow host submission", "Slow GPU submission"),
    ("no host texture", "Texture rejected"),
    ("D3D12:", "Direct3D validation message"),
    ("is not supported", "Unsupported operation"),
)


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def resolve_engine(version):
    release_exe = ENGINE / "target" / "release" / "victory.exe"
    diag_exe = ENGINE / "target" / "diag" / "victory.exe"
    debug_exe = ENGINE / "target" / "debug" / "victory.exe"
    if not version or version == "victory.exe":
        if release_exe.is_file():
            return release_exe
        return diag_exe if diag_exe.is_file() else debug_exe
    if version == "victory-diag.exe":
        return diag_exe
    if version == "victory-debug.exe":
        return debug_exe
    return ENGINE / "launcher" / "versions" / Path(version).name


def resolve_title():
    state_file = LOGDIR / "last_title.txt"
    try:
        if state_file.is_file():
            saved = Path(state_file.read_text(encoding="utf-8").strip())
            if saved.is_file() and saved.suffix.lower() == ".exe":
                return saved
    except OSError:
        pass
    desktop = Path.home() / "Desktop"
    direct = desktop / "GTA6.Xbox.v1.0.2.1-CYBERLEEK" / "game_scarlett_master.exe"
    if direct.is_file():
        return direct
    if desktop.is_dir():
        for folder in sorted(desktop.glob("GTA6*")):
            exe = folder / "game_scarlett_master.exe"
            if exe.is_file():
                return exe
    return None


def file_meta(path, alias=None):
    try:
        st = path.stat()
        return {
            "name": alias or path.name,
            "date": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size": st.st_size,
        }
    except OSError:
        return None


def list_versions():
    release_exe = ENGINE / "target" / "release" / "victory.exe"
    diag_exe = ENGINE / "target" / "diag" / "victory.exe"
    debug_exe = ENGINE / "target" / "debug" / "victory.exe"
    primary = release_exe if release_exe.is_file() else (diag_exe if diag_exe.is_file() else debug_exe)
    items = []
    if primary.is_file():
        items.append(file_meta(primary, alias="victory.exe"))
    if release_exe.is_file() and diag_exe.is_file():
        items.append(file_meta(diag_exe, alias="victory-diag.exe"))
    if debug_exe.is_file() and primary != debug_exe:
        items.append(file_meta(debug_exe, alias="victory-debug.exe"))
    vdir = ENGINE / "launcher" / "versions"
    if vdir.is_dir():
        for p in sorted(vdir.glob("*.exe"), reverse=True):
            items.append(file_meta(p))
    return [x for x in items if x]


def is_running():
    with PROC_LOCK:
        return PROC is not None and PROC.poll() is None


def stop_game():
    with PROC_LOCK:
        proc = PROC
    if proc is not None and proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    return False


def parse_line_metrics(line, target):
    if "D3D12:" in line:
        target["counts"]["validation"] = target["counts"].get("validation", 0) + 1
    if "is not supported" in line:
        target["counts"]["unsupported"] = target["counts"].get("unsupported", 0) + 1
    if "host pipeline refused" in line or "host mesh pipeline refused" in line:
        target["counts"]["rejected"] = target["counts"].get("rejected", 0) + 1
    if "slow host submission" in line:
        target["counts"]["slow"] = target["counts"].get("slow", 0) + 1
    if "host GPU failure" in line:
        target["counts"]["fatal"] = target["counts"].get("fatal", 0) + 1
    if line.startswith("Exit code:"):
        target["exit"] = line.split(":", 1)[1].strip()
    elif line.startswith("VIce session "):
        target["start"] = line.removeprefix("VIce session ").strip()
    m = FPS_RE.search(line)
    if m:
        target["fps"] = float(m.group(1))
        target["ms"] = float(m.group(2))
        target["draws"] = int(m.group(3))
        if m.group(4) is not None:
            target["dropped"] = int(m.group(4))


def read_tail_lines(path, max_bytes=393216, max_lines=500):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                raw = f.read()
                nl = raw.find(b"\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
            else:
                raw = f.read()
        text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def read_disk_session(filename):
    path = LOGDIR / filename
    try:
        st = path.stat()
    except OSError:
        return {
            "meta": None,
            "start": None,
            "exit": None,
            "fps": None,
            "ms": None,
            "draws": None,
            "dropped": None,
            "counts": {},
            "lines": [],
        }

    sig = (st.st_mtime_ns, st.st_size)
    with DISK_CACHE_LOCK:
        cached = DISK_CACHE.get(filename)
        if cached and cached[0] == sig:
            return cached[1]

    lines = read_tail_lines(path, max_bytes=524288, max_lines=600)
    # Grab header start timestamp if truncated
    start_ts = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
            if first.startswith("VIce session "):
                start_ts = first.removeprefix("VIce session ").strip()
    except OSError:
        pass

    summary = {
        "meta": {
            "name": path.name,
            "date": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size": st.st_size,
        },
        "start": start_ts,
        "exit": None,
        "fps": None,
        "ms": None,
        "draws": None,
        "dropped": None,
        "counts": {},
        "lines": lines,
    }
    for line in lines:
        parse_line_metrics(line, summary)
    if start_ts and not summary["start"]:
        summary["start"] = start_ts

    with DISK_CACHE_LOCK:
        DISK_CACHE[filename] = (sig, summary)
    return summary


def current_session_summary(include_lines=False):
    if is_running():
        with LIVE_LOCK:
            data = {
                "meta": file_meta(LOGDIR / "play.log"),
                "start": LIVE_STATE["start"],
                "exit": LIVE_STATE["exit"],
                "fps": LIVE_STATE["fps"],
                "ms": LIVE_STATE["ms"],
                "draws": LIVE_STATE["draws"],
                "dropped": LIVE_STATE["dropped"],
                "counts": dict(LIVE_STATE["counts"]),
            }
            if include_lines:
                data["lines"] = list(LIVE_LINES)
            return data

    disk = read_disk_session("play.log")
    if include_lines:
        return dict(disk)
    out = dict(disk)
    out.pop("lines", None)
    return out


def build_state():
    title = resolve_title()
    return {
        "server": SERVER_VERSION,
        "running": is_running(),
        "title": str(title) if title else None,
        "titleName": title.name if title else None,
        "versions": list_versions(),
        "current": current_session_summary(include_lines=False),
    }


def launch_game(cfg):
    global PROC
    engine = resolve_engine(cfg.get("version", "victory.exe"))
    if not engine.is_file():
        raise ValueError(f"Exécutable moteur introuvable : {engine.name}")
    title = resolve_title()
    if title is None or not title.is_file():
        raise ValueError("Jeu introuvable (game_scarlett_master.exe).")

    console = cfg.get("console", "series-x")
    if console not in ("series-x", "series-s"):
        raise ValueError("Profil console invalide.")

    cmd = [str(engine), "run", str(title), "--execute", "--console", console]
    env = os.environ.copy()
    env["NO_COLOR"] = "1"

    fast_gpu = bool(cfg.get("fastGpu", True))
    dstorage_notify = bool(cfg.get("dstorageNotify", True))

    if fast_gpu:
        env["VICTORY_RELAY"] = "2"
        env["VICTORY_WIDE_BACK"] = "1"
        env["VICTORY_LATE_WAIT_LISTS"] = "512"
        env["VICTORY_CARD_BUFFERS_MIN_MB"] = "1"
        env["VICTORY_CARD_BUFFERS_MB"] = "768"
    else:
        env["VICTORY_RELAY"] = "0"
        env["VICTORY_WIDE_BACK"] = "0"
        env["VICTORY_LATE_WAIT_LISTS"] = "128"
        env["VICTORY_CARD_BUFFERS_MIN_MB"] = "16"
        env["VICTORY_CARD_BUFFERS_MB"] = "512"

    env["VICTORY_DSTORAGE_NOTIFY"] = "1" if dstorage_notify else "0"

    for env_key, opt_key in (
        ("VICTORY_D3D12_WARP", "warp"),
        ("VICTORY_D3D12_DEBUG", "debug"),
        ("VICE_SURFACE_STATS", "surface"),
    ):
        env.pop(env_key, None)
        if cfg.get(opt_key):
            env[env_key] = "1"

    with PROC_LOCK:
        if PROC is not None and PROC.poll() is None:
            raise RuntimeError("Le jeu est déjà en cours d’exécution.")
        LOGDIR.mkdir(parents=True, exist_ok=True)
        current = LOGDIR / "play.log"
        if current.exists():
            try:
                current.replace(LOGDIR / "play.previous.log")
            except OSError:
                pass
        proc = subprocess.Popen(
            cmd,
            cwd=str(ENGINE),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        PROC = proc

    now_iso = datetime.now().isoformat(timespec="seconds")
    with LIVE_LOCK:
        LIVE_LINES.clear()
        LIVE_STATE.update(
            {
                "start": now_iso,
                "exit": None,
                "fps": None,
                "ms": None,
                "draws": None,
                "dropped": None,
                "counts": {},
            }
        )

    def reader():
        global PROC
        counts = Counter()
        code = 1
        try:
            with (LOGDIR / "play.log").open("w", encoding="utf-8") as log:
                header = [
                    f"VIce session {now_iso}",
                    f"Engine: {engine}",
                    f"Engine build: {datetime.fromtimestamp(engine.stat().st_mtime).isoformat(timespec='seconds')}",
                    f"Console: {console} | FastGPU: {int(fast_gpu)} | DStorageNotify: {int(dstorage_notify)}",
                    f"Direct3D debug: {env.get('VICTORY_D3D12_DEBUG', '0')} | Surface: {env.get('VICE_SURFACE_STATS', '0')} | WARP: {env.get('VICTORY_D3D12_WARP', '0')}",
                ]
                for h in header:
                    log.write(h + "\n")
                    with LIVE_LOCK:
                        LIVE_LINES.append(h)
                log.flush()
                for raw in proc.stdout:
                    line = ANSI.sub("", raw).rstrip("\r\n")
                    for text, label in LOG_CATEGORIES:
                        if text in line:
                            counts[label] += 1
                            break
                    log.write(line + "\n")
                    log.flush()
                    with LIVE_LOCK:
                        LIVE_LINES.append(line)
                        parse_line_metrics(line, LIVE_STATE)
                code = proc.wait()
                summary = (
                    "\nDIAGNOSTIC SUMMARY\n"
                    + "\n".join(f"{label}: {n}" for label, n in counts.most_common())
                    + f"\nExit code: {code}\n"
                )
                log.write(summary)
                log.flush()
                with LIVE_LOCK:
                    LIVE_STATE["exit"] = str(code)
                    LIVE_LINES.append(f"Exit code: {code}")
        except OSError:
            pass
        finally:
            with PROC_LOCK:
                if PROC is proc:
                    PROC = None

    threading.Thread(target=reader, daemon=True).start()


def filter_logs(lines, query="", severity="all", limit=350):
    q = query.strip().lower()
    out = []
    for idx, line in enumerate(lines, 1):
        if "ERROR" in line or "host GPU failure" in line:
            lvl = "error"
        elif "WARN" in line or "D3D12:" in line:
            lvl = "warn"
        elif "fps," in line and "ms/frame" in line:
            lvl = "perf"
        elif "INFO" in line:
            lvl = "info"
        else:
            lvl = "other"
        if severity != "all" and lvl != severity:
            continue
        if q and q not in line.lower():
            continue
        out.append({"n": idx, "lvl": lvl, "text": line})
    total = len(out)
    return out[-limit:], total


class Handler(BaseHTTPRequestHandler):
    def local_host(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def check_token(self):
        if self.headers.get("X-Studio-Key") != TOKEN:
            self.send_json(401, {"error": "Session expirée", "code": "token_expired"})
            return False
        return True

    def do_GET(self):
        if not self.local_host():
            self.send_json(403, {"error": "Accès local uniquement."})
            return
        req = urlsplit(self.path)
        if req.path == "/":
            html = (BASE / "studio.html").read_text(encoding="utf-8").replace("__STUDIO_SESSION_TOKEN__", TOKEN)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)
            return
        if req.path in ("/health", "/api/health"):
            self.send_bytes(200, "text/plain; charset=utf-8", SERVER_VERSION.encode("utf-8"))
            return
        if req.path == "/api/token":
            self.send_json(200, {"token": TOKEN, "server": SERVER_VERSION})
            return
        if req.path in ("/art/sunset.svg", "/art/vice-logo.svg"):
            asset = BASE / req.path.rsplit("/", 1)[1]
            if asset.is_file():
                self.send_bytes(200, "image/svg+xml", asset.read_bytes())
                return
        if req.path == "/api/state":
            if not self.check_token():
                return
            self.send_json(200, build_state())
            return
        if req.path == "/api/logs":
            if not self.check_token():
                return
            qs = parse_qs(req.query)
            q = (qs.get("q") or [""])[0]
            sev = (qs.get("level") or ["all"])[0]
            try:
                limit = max(50, min(1000, int((qs.get("limit") or ["350"])[0])))
            except ValueError:
                limit = 350
            session = current_session_summary(include_lines=True)
            lines = session.get("lines", [])
            entries, total = filter_logs(lines, query=q, severity=sev, limit=limit)
            self.send_json(200, {"entries": entries, "matched": total, "total": len(lines)})
            return
        self.send_json(404, {"error": "Route introuvable."})

    def do_POST(self):
        if not self.local_host():
            self.send_json(403, {"error": "Accès local uniquement."})
            return
        req = urlsplit(self.path)
        if not self.check_token():
            return

        if req.path == "/api/stop":
            stopped = stop_game()
            self.send_json(200, {"stopped": stopped})
            return

        if req.path == "/api/open-log-dir":
            LOGDIR.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(str(LOGDIR))
            except OSError as exc:
                self.send_json(500, {"error": str(exc)})
                return
            self.send_json(200, {"ok": True})
            return

        if req.path in ("/api/launch", "/api/open-launcher"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 8192:
                    raise ValueError("Requête invalide.")
                cfg = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(cfg, dict):
                    raise ValueError("Configuration invalide.")
                launch_game(cfg)
                self.send_json(200, {"ok": True, "running": True})
            except RuntimeError as exc:
                self.send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, OSError, KeyError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            return

        self.send_json(404, {"error": "Route introuvable."})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        st = build_state()
        assert "versions" in st and "current" in st
        print("Self-test OK:", len(st["versions"]), "builds, title=", st["titleName"])
        sys.exit(0)

    port = 0 if "--no-browser" in sys.argv else 17642
    existing = False
    server = None
    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        if not port:
            raise
        from urllib.request import urlopen

        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                existing = b"VIce Studio local" in r.read()
        except OSError:
            pass
        if not existing:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "Le port local 17642 de VIce Studio est occupé. Fermez l'autre application puis réessayez.",
                "VIce Studio",
                0x10,
            )
            sys.exit(1)

    url = f"http://127.0.0.1:{port if existing else server.server_port}/"
    if "--no-browser" in sys.argv:
        print(url + f"?key={TOKEN}", flush=True)
    if "--no-browser" not in sys.argv and "--headless" not in sys.argv:
        chrome = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"
        if chrome.is_file():
            subprocess.Popen(
                [
                    str(chrome),
                    "--app=" + url,
                    "--window-size=980,720",
                    "--user-data-dir=" + str(Path(os.environ["LOCALAPPDATA"]) / "VIce" / "studio-browser"),
                ]
            )
        else:
            import webbrowser

            webbrowser.open(url)
    if not existing:
        server.serve_forever()
