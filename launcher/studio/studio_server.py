from collections import Counter, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
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
AUTH_FILE = Path(os.environ["LOCALAPPDATA"]) / "VIce" / "studio-auth.json"
LOGDIR = Path(os.environ["LOCALAPPDATA"]) / "VIce" / "launcher"

SERVER_VERSION = "VIce Studio local v4"
TOKEN = secrets.token_urlsafe(24)

DEV_LOCK = threading.Lock()
DEV = False
LAST_ATTEMPT = 0.0

PROC_LOCK = threading.Lock()
PROC = None
LIVE_LOCK = threading.Lock()
LIVE_LINES = deque(maxlen=600)
LIVE_STATE = {
    "start": None,
    "exit": None,
    "fps": None,
    "ms": None,
    "draws": None,
    "dropped": None,
    "maxFrame": None,
    "counts": {},
    "codes": {},
}

DISK_CACHE_LOCK = threading.Lock()
DISK_CACHE = {}

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FPS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s+fps,\s+(\d+(?:\.\d+)?)\s+ms/frame.*?(\d+)\s+draws/frame(?:,\s+dropped\s+(\d+))?"
)
FRAME_RE = re.compile(r"\bframe=(\d+)")
ID_RE = re.compile(r"id=(\d+)")
LOG_CATEGORIES = (
    ("host pipeline refused", "Pipeline rejected"),
    ("host mesh pipeline refused", "Mesh pipeline rejected"),
    ("host root signature refused", "Root signature rejected"),
    ("slow host submission", "Slow GPU submission"),
    ("no host texture", "Texture rejected"),
    ("D3D12:", "Direct3D validation message"),
    ("is not supported", "Unsupported operation"),
)

# Win32 helpers for fast real-time game process & window detection (zero subprocess spam)
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_WM_CLOSE = 0x0010
_SW_RESTORE = 9
_SW_SHOW = 5
_HWND_TOPMOST = wt.HWND(-1)
_HWND_NOTOPMOST = wt.HWND(-2)
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_SHOWWINDOW = 0x0040

_user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
_user32.AllowSetForegroundWindow.restype = wt.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_user32.AttachThreadInput.restype = wt.BOOL
_user32.IsIconic.argtypes = [wt.HWND]
_user32.IsIconic.restype = wt.BOOL
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wt.BOOL
_user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
_user32.SetWindowPos.restype = wt.BOOL
_user32.BringWindowToTop.argtypes = [wt.HWND]
_user32.BringWindowToTop.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wt.DWORD


def _bring_to_front(hwnd, pid):
    attached_fg = False
    attached_target = False
    cur_tid = _kernel32.GetCurrentThreadId()
    fg_tid = 0
    target_tid = 0
    try:
        hwnd_v = wt.HWND(hwnd)
        _user32.AllowSetForegroundWindow(pid)
        fg = _user32.GetForegroundWindow()
        if fg:
            fg_tid = _user32.GetWindowThreadProcessId(fg, None)
        target_tid = _user32.GetWindowThreadProcessId(hwnd_v, None)
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid and target_tid != fg_tid:
            attached_target = bool(_user32.AttachThreadInput(cur_tid, target_tid, True))
        if _user32.IsIconic(hwnd_v):
            _user32.ShowWindow(hwnd_v, _SW_RESTORE)
        else:
            _user32.ShowWindow(hwnd_v, _SW_SHOW)
        # Brief topmost toggle + thread input attach reliably raises above Chrome on Windows 11.
        _user32.SetWindowPos(hwnd_v, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.SetWindowPos(hwnd_v, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        _user32.BringWindowToTop(hwnd_v)
        _user32.SetForegroundWindow(hwnd_v)
    except OSError:
        pass
    finally:
        if attached_target:
            _user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            _user32.AttachThreadInput(cur_tid, fg_tid, False)


def _pid_exe_name(pid):
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        _kernel32.CloseHandle(h)
    return ""


def _find_game_windows():
    """Return list of (hwnd, pid, title) for visible victory.exe game windows."""
    found = []
    with PROC_LOCK:
        tracked_pid = PROC.pid if (PROC is not None and PROC.poll() is None) else None

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        p = pid.value
        if not p:
            return True
        is_game_pid = (tracked_pid is not None and p == tracked_pid)
        if not is_game_pid:
            exe = _pid_exe_name(p)
            if exe in ("victory.exe", "victory-diag.exe", "victory-debug.exe") or exe.startswith("vice-"):
                is_game_pid = True
        if is_game_pid:
            length = _user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                found.append((hwnd, p, buf.value))
            else:
                found.append((hwnd, p, "VIce"))
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    except OSError:
        pass
    return found


def game_status():
    """Return (running, window_open, window_title, pids)."""
    with PROC_LOCK:
        proc_alive = PROC is not None and PROC.poll() is None
        tracked_pid = PROC.pid if proc_alive else None
    wins = _find_game_windows()
    pids = sorted({w[1] for w in wins} | ({tracked_pid} if tracked_pid else set()))
    window_open = len(wins) > 0
    window_title = wins[0][2] if wins else None
    running = proc_alive or window_open
    return running, window_open, window_title, pids


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def verify_password(password):
    if not isinstance(password, str) or len(password) > 256:
        return False
    try:
        cfg = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(cfg["salt"]), 600000
        )
        return hmac.compare_digest(calculated, bytes.fromhex(cfg["hash"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
    running, _, _, _ = game_status()
    return running


def stop_game():
    wins = _find_game_windows()
    _, _, _, pids = game_status()
    # Try graceful window close first so pipeline cache saves cleanly!
    for hwnd, _, _ in wins:
        try:
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        except OSError:
            pass
    if wins:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            r, _, _, _ = game_status()
            if not r:
                return True
            time.sleep(0.15)
    # Fallback force kill for any remaining game PIDs
    _, _, _, rem_pids = game_status()
    killed = False
    for pid in rem_pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        killed = True
    return killed or bool(pids)


def parse_line_metrics(line, target):
    if "D3D12:" in line:
        target["counts"]["validation"] = target["counts"].get("validation", 0) + 1
        m = ID_RE.search(line)
        if m:
            target["codes"][m.group(1)] = target["codes"].get(m.group(1), 0) + 1
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
    fm = FRAME_RE.search(line)
    if fm:
        val = int(fm.group(1))
        if target["maxFrame"] is None or val > target["maxFrame"]:
            target["maxFrame"] = val
    m = FPS_RE.search(line)
    if m:
        target["fps"] = float(m.group(1))
        target["ms"] = float(m.group(2))
        target["draws"] = int(m.group(3))
        if m.group(4) is not None:
            target["dropped"] = int(m.group(4))


def read_tail_lines(path, max_bytes=393216, max_lines=600):
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
        return text.splitlines()[-max_lines:]
    except OSError:
        return []


def empty_session():
    return {
        "meta": None,
        "start": None,
        "exit": None,
        "fps": None,
        "ms": None,
        "draws": None,
        "dropped": None,
        "maxFrame": None,
        "counts": {},
        "codes": {},
        "lines": [],
    }


def read_disk_session(filename):
    path = LOGDIR / filename
    try:
        st = path.stat()
    except OSError:
        return empty_session()

    sig = (st.st_mtime_ns, st.st_size)
    with DISK_CACHE_LOCK:
        cached = DISK_CACHE.get(filename)
        if cached and cached[0] == sig:
            return cached[1]

    lines = read_tail_lines(path, max_bytes=524288, max_lines=600)
    start_ts = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
            if first.startswith("VIce session "):
                start_ts = first.removeprefix("VIce session ").strip()
    except OSError:
        pass

    summary = empty_session()
    summary["meta"] = {
        "name": path.name,
        "date": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "size": st.st_size,
    }
    summary["start"] = start_ts
    summary["lines"] = lines
    for line in lines:
        parse_line_metrics(line, summary)
    if start_ts and not summary["start"]:
        summary["start"] = start_ts

    with DISK_CACHE_LOCK:
        DISK_CACHE[filename] = (sig, summary)
    return summary


def current_session_summary(include_lines=False):
    with PROC_LOCK:
        proc_owned = PROC is not None and PROC.poll() is None
    if proc_owned:
        with LIVE_LOCK:
            data = {
                "meta": file_meta(LOGDIR / "play.log"),
                "start": LIVE_STATE["start"],
                "exit": LIVE_STATE["exit"],
                "fps": LIVE_STATE["fps"],
                "ms": LIVE_STATE["ms"],
                "draws": LIVE_STATE["draws"],
                "dropped": LIVE_STATE["dropped"],
                "maxFrame": LIVE_STATE["maxFrame"],
                "counts": dict(LIVE_STATE["counts"]),
                "codes": dict(LIVE_STATE["codes"]),
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
    with DEV_LOCK:
        dev = DEV
    title = resolve_title()
    versions = list_versions()
    running, window_open, window_title, _ = game_status()
    cur = current_session_summary(include_lines=False)
    prev = dict(read_disk_session("play.previous.log"))
    prev.pop("lines", None)

    if not dev:
        versions = [v for v in versions if v["name"] == "victory.exe"]
        cur = {
            "meta": cur["meta"],
            "start": cur["start"],
            "exit": cur["exit"],
            "fps": cur["fps"],
            "ms": cur["ms"],
            "draws": None,
            "dropped": None,
            "maxFrame": None,
            "counts": {},
            "codes": {},
        }
        prev = {
            "meta": None,
            "start": None,
            "exit": None,
            "fps": None,
            "ms": None,
            "draws": None,
            "dropped": None,
            "maxFrame": None,
            "counts": {},
            "codes": {},
        }

    return {
        "server": SERVER_VERSION,
        "developer": dev,
        "running": running,
        "windowOpen": window_open,
        "windowTitle": window_title,
        "hasEmblem": (BASE / "studio-emblem.png").is_file(),
        "title": str(title) if (title and dev) else bool(title),
        "titleName": title.name if title else None,
        "versions": versions,
        "current": cur,
        "previous": prev,
    }


def launch_game(cfg, developer):
    global PROC
    running, _, _, _ = game_status()
    if running:
        raise RuntimeError("Le jeu est déjà ouvert.")

    version = cfg.get("version", "victory.exe")
    if not developer:
        version = "victory.exe"
    engine = resolve_engine(version)
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

    fast_gpu = bool(cfg.get("fastGpu", True)) if developer else True
    dstorage_notify = bool(cfg.get("dstorageNotify", True)) if developer else True
    bvh_builder = bool(cfg.get("bvhBuilder", True))

    if fast_gpu:
        # RTX 4070 8 GB / 16 GB RAM optimal set (fxceless 3973179 + #854/#993/#994):
        # - LEVELS_ON_DEMAND=1 saves ~3.1 GB VRAM for unstreamed mip levels
        # - RELAY=1 + ASYNC_SUBMIT=1 + QUEUE2=1 + INDIRECT_WINDOW=1 + COPY_PLAIN=1
        #   + JOIN_PARTS=1 + BACK_OWN_VIEW=1 + TEXTURE_NO_WAIT=1 (7-9.6 fps on RTX 4070)
        # - DRIVER_WATCH stays ON by default to disable INDIRECT_WINDOW before any TDR
        env["VICTORY_LEVELS_ON_DEMAND"] = "1"
        env["VICTORY_RELAY"] = "1"
        env["VICTORY_ASYNC_SUBMIT"] = "1"
        env["VICTORY_QUEUE2"] = "1"
        env["VICTORY_INDIRECT_WINDOW"] = "1"
        env["VICTORY_COPY_PLAIN"] = "1"
        env["VICTORY_JOIN_PARTS"] = "1"
        env["VICTORY_BACK_OWN_VIEW"] = "1"
        env["VICTORY_TEXTURE_NO_WAIT"] = "1"
        env["VICTORY_WIDE_BACK"] = "1"
        env["VICTORY_CARD_REHASH_RUNS"] = "1"
        env["VICTORY_LATE_WAIT_LISTS"] = "512"
        env["VICTORY_CARD_BUFFERS_MIN_MB"] = "1"
        env["VICTORY_CARD_BUFFERS_MB"] = "512"
        env["VICTORY_TEXTURE_ROOM_PERCENT"] = "94"
        env["VICTORY_CARD_ROOM_FOR_TARGETS_MB"] = "2048"
        env["VICTORY_PIPELINE_COMMIT_MB"] = "768"
        env["VICTORY_PIPELINE_WAIT_MS"] = "1000"
    else:
        env["VICTORY_LEVELS_ON_DEMAND"] = "1"
        env["VICTORY_RELAY"] = "0"
        env["VICTORY_ASYNC_SUBMIT"] = "0"
        env["VICTORY_QUEUE2"] = "0"
        env["VICTORY_INDIRECT_WINDOW"] = "0"
        env["VICTORY_COPY_PLAIN"] = "0"
        env["VICTORY_JOIN_PARTS"] = "0"
        env["VICTORY_BACK_OWN_VIEW"] = "0"
        env["VICTORY_WIDE_BACK"] = "0"
        env["VICTORY_CARD_REHASH_RUNS"] = "1"
        env["VICTORY_LATE_WAIT_LISTS"] = "128"
        env["VICTORY_CARD_BUFFERS_MIN_MB"] = "16"
        env["VICTORY_CARD_BUFFERS_MB"] = "512"
        env["VICTORY_TEXTURE_ROOM_PERCENT"] = "94"
        env["VICTORY_CARD_ROOM_FOR_TARGETS_MB"] = "2048"
        env["VICTORY_PIPELINE_COMMIT_MB"] = "768"
        env["VICTORY_PIPELINE_WAIT_MS"] = "1000"

    env["VICTORY_BVH_BUILDER"] = "1" if bvh_builder else "0"
    env["VICTORY_PAGE_WRITES"] = "1" if dstorage_notify else "0"
    env["VICTORY_DSTORAGE_NOTIFY"] = "1" if dstorage_notify else "0"

    for env_key, opt_key in (
        ("VICTORY_D3D12_WARP", "warp"),
        ("VICTORY_D3D12_DEBUG", "debug"),
        ("VICE_SURFACE_STATS", "surface"),
    ):
        env.pop(env_key, None)
        if developer and cfg.get(opt_key):
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
        try:
            _user32.AllowSetForegroundWindow(proc.pid)
        except OSError:
            pass

    def focus_when_ready():
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            for hwnd, pid, _ in _find_game_windows():
                if pid == proc.pid:
                    _bring_to_front(hwnd, pid)
                    time.sleep(0.35)
                    if proc.poll() is None and _user32.GetForegroundWindow() != hwnd:
                        _bring_to_front(hwnd, pid)
                    return
            time.sleep(0.25)

    threading.Thread(target=focus_when_ready, daemon=True).start()

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
                "maxFrame": None,
                "counts": {},
                "codes": {},
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
    return out[-limit:], len(out)


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
        if req.path == "/art/studio-emblem.png":
            asset = BASE / "studio-emblem.png"
            if asset.is_file():
                self.send_bytes(200, "image/png", asset.read_bytes())
                return
        if req.path == "/api/state":
            if not self.check_token():
                return
            self.send_json(200, build_state())
            return
        if req.path == "/api/logs":
            if not self.check_token():
                return
            with DEV_LOCK:
                dev = DEV
            if not dev:
                self.send_json(403, {"error": "Mode développeur requis."})
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
        global DEV, LAST_ATTEMPT
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
            with DEV_LOCK:
                dev = DEV
            if not dev:
                self.send_json(403, {"error": "Mode développeur requis."})
                return
            LOGDIR.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(str(LOGDIR))
            except OSError as exc:
                self.send_json(500, {"error": str(exc)})
                return
            self.send_json(200, {"ok": True})
            return

        if req.path == "/api/developer":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 4096:
                    raise ValueError("Requête invalide.")
                cfg = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(cfg, dict):
                    raise ValueError("Configuration invalide.")
                with DEV_LOCK:
                    if cfg.get("enabled") is False:
                        DEV = False
                    elif cfg.get("enabled") is True:
                        now = time.monotonic()
                        if now - LAST_ATTEMPT < 1.0:
                            self.send_json(429, {"error": "Réessaie dans un instant."})
                            return
                        LAST_ATTEMPT = now
                        if not verify_password(cfg.get("password")):
                            self.send_json(403, {"error": "Mot de passe incorrect."})
                            return
                        DEV = True
                    else:
                        raise ValueError("Mode invalide.")
                self.send_json(200, {"developer": DEV})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if req.path in ("/api/launch", "/api/open-launcher"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 8192:
                    raise ValueError("Requête invalide.")
                cfg = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(cfg, dict):
                    raise ValueError("Configuration invalide.")
                with DEV_LOCK:
                    dev = DEV
                launch_game(cfg, dev)
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
        assert "versions" in st and "windowOpen" in st and "developer" in st
        print("Self-test OK:", len(st["versions"]), "builds, windowOpen=", st["windowOpen"])
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
            import ctypes as _ct

            _ct.windll.user32.MessageBoxW(
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
                    "--window-size=1120,760",
                    "--user-data-dir=" + str(Path(os.environ["LOCALAPPDATA"]) / "VIce" / "studio-browser"),
                ]
            )
        else:
            import webbrowser

            webbrowser.open(url)
    if not existing:
        server.serve_forever()
