"""
VIce Clip Overlay & Hardware H.264 Screen Recorder (temporary showcase tool).

Displays a sleek click-through GTA VI / VIce telemetry HUD directly over the
game window (Live FPS, Frametime + live sparkline, GPU % via native nvml.dll,
Temp, VRAM, CPU %, RAM) and records clean H.264 MP4 clips of the game window
(including the overlay) using Windows Media Foundation hardware encoding.

Hotkeys (work while playing):
  F9  : Start / Stop MP4 recording (saved to %USERPROFILE%\\Videos\\VIce Clips)
  F10 : Show / Hide overlay
  F11 : Quit cleanly (finalizes any active recording)
"""

from collections import deque
from datetime import datetime
from pathlib import Path
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import threading
import time
import tkinter as tk

import cv2
import mss
import numpy as np
import psutil

# ---- Strict 64-bit Win32 API bindings (no truncated 32-bit HWND/HANDLE!) ----
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = wt.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A

_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_user32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
_user32.EnumWindows.restype = wt.BOOL
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsWindowVisible.restype = wt.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.GetClientRect.restype = wt.BOOL
_user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_user32.ClientToScreen.restype = wt.BOOL
_user32.GetParent.argtypes = [wt.HWND]
_user32.GetParent.restype = wt.HWND
_user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT
]
_user32.SetWindowPos.restype = wt.BOOL
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short

TITLE_FPS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*fps\s+(\d+(?:\.\d+)?)\s*ms/frame(?:\s+(\d+)\s*draws/frame)?",
    re.IGNORECASE,
)
LOG_FPS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s+fps,\s+(\d+(?:\.\d+)?)\s+ms/frame.*?(\d+)\s+draws/frame",
    re.IGNORECASE,
)

PLAY_LOG = Path(os.environ.get("LOCALAPPDATA", "")) / "VIce" / "launcher" / "play.log"
CLIPS_DIR = Path.home() / "Videos" / "VIce Clips"
GAME_EXES = {"victory.exe", "victory_sm.exe", "victory-diag.exe", "victory-debug.exe"}


# ---- Native NVML GPU reader (~1 ms, zero subprocess overhead!) ----
class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NvmlReader:
    def __init__(self):
        self.lib = None
        self.handle = ctypes.c_void_p()
        self.name = "RTX 4070 Laptop"
        try:
            lib = ctypes.WinDLL("nvml.dll")
            lib.nvmlInit_v2.restype = ctypes.c_int
            lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
            lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
            lib.nvmlDeviceGetName.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
            lib.nvmlDeviceGetName.restype = ctypes.c_int
            lib.nvmlDeviceGetUtilizationRates.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NvmlUtil)]
            lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
            lib.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NvmlMem)]
            lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            lib.nvmlDeviceGetTemperature.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
            lib.nvmlDeviceGetTemperature.restype = ctypes.c_int

            if lib.nvmlInit_v2() == 0 and lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.handle)) == 0:
                self.lib = lib
                buf = ctypes.create_string_buffer(96)
                if lib.nvmlDeviceGetName(self.handle, buf, 96) == 0:
                    raw = buf.value.decode("utf-8", errors="replace").strip()
                    self.name = raw.replace("NVIDIA GeForce ", "").replace(" GPU", "") or self.name
        except Exception:
            self.lib = None

    def read(self):
        """Return (name, gpu_util_pct, vram_used_gb, vram_total_gb, temp_c) or None."""
        if self.lib is not None:
            try:
                u = _NvmlUtil()
                m = _NvmlMem()
                temp = ctypes.c_uint(0)
                if (
                    self.lib.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(u)) == 0
                    and self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(m)) == 0
                ):
                    self.lib.nvmlDeviceGetTemperature(self.handle, 0, ctypes.byref(temp))
                    return (
                        self.name,
                        float(u.gpu),
                        m.used / (1024 ** 3),
                        m.total / (1024 ** 3),
                        int(temp.value),
                    )
            except Exception:
                pass
        return None


def find_game_processes():
    """Return dict {pid: exe_name_lower} for any running victory/vice game engine."""
    found = {}
    try:
        for p in psutil.process_iter(["pid", "name"]):
            nm = (p.info.get("name") or "").lower()
            if nm in GAME_EXES or nm.startswith("vice-"):
                found[p.info["pid"]] = nm
    except Exception:
        pass
    return found


def find_game_window(game_pids):
    """Return (hwnd, title, client_rect=(x, y, w, h), exe) for the largest visible game window."""
    candidates = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = game_pids.get(pid.value, "")

        length = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        tlow = title.lower()

        is_game = bool(exe) or ("americas | victory" in tlow) or ("| victory " in tlow)
        if not is_game:
            return True

        crect = wt.RECT()
        pt = wt.POINT(0, 0)
        if _user32.GetClientRect(hwnd, ctypes.byref(crect)) and _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            w = crect.right - crect.left
            h = crect.bottom - crect.top
            if w >= 320 and h >= 200:
                candidates.append((w * h, hwnd, title, (pt.x, pt.y, w, h), exe or "victory.exe"))
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        pass

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, hwnd, title, rect, exe = candidates[0]
    return hwnd, title, rect, exe


def read_recent_log_fps():
    """Read latest FPS line from play.log if updated recently (< 15s)."""
    try:
        if not PLAY_LOG.is_file():
            return None
        st = PLAY_LOG.stat()
        if (time.time() - st.st_mtime) > 15.0:
            return None
        with PLAY_LOG.open("rb") as f:
            if st.st_size > 32768:
                f.seek(st.st_size - 32768)
            chunk = f.read().decode("utf-8", errors="replace")
        matches = LOG_FPS_RE.findall(chunk)
        if matches:
            fps, ms, draws = matches[-1]
            return float(fps), float(ms), int(draws)
    except OSError:
        pass
    return None


class TelemetryCollector:
    """Fast background worker sampling NVML GPU, CPU, RAM, and game FPS at 4 Hz."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.nvml = NvmlReader()
        self.data = {
            "proc_running": False,
            "game_open": False,
            "game_rect": None,
            "smooth_motion": False,
            "fps": 0.0,
            "ms": 0.0,
            "draws": 0,
            "gpu_name": self.nvml.name,
            "gpu_util": 0.0,
            "gpu_temp": 0,
            "vram_used_gb": 0.0,
            "vram_total_gb": 8.0,
            "cpu_util": 0.0,
            "ram_used_gb": 0.0,
            "ram_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
            "history_ms": deque([0.0] * 48, maxlen=48),
            "ticks": 0,
        }
        psutil.cpu_percent(interval=None)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            game_pids = find_game_processes()
            win = find_game_window(game_pids)
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            ram_used = vm.used / (1024 ** 3)
            ram_total = vm.total / (1024 ** 3)
            nv = self.nvml.read()

            fps_val = None
            ms_val = None
            draws_val = None
            rect = None
            smooth = any(nm == "victory_sm.exe" for nm in game_pids.values())
            game_open = False

            if win is not None:
                _, title, rect, exe = win
                game_open = True
                if exe == "victory_sm.exe":
                    smooth = True
                m = TITLE_FPS_RE.search(title)
                if m:
                    fps_val = float(m.group(1))
                    ms_val = float(m.group(2))
                    draws_val = int(m.group(3)) if m.group(3) else 0
                else:
                    fb = read_recent_log_fps()
                    if fb:
                        fps_val, ms_val, draws_val = fb
            elif game_pids:
                fb = read_recent_log_fps()
                if fb:
                    fps_val, ms_val, draws_val = fb

            with self.lock:
                self.data["proc_running"] = bool(game_pids) or game_open
                self.data["game_open"] = game_open
                self.data["game_rect"] = rect
                self.data["smooth_motion"] = smooth
                self.data["cpu_util"] = cpu
                self.data["ram_used_gb"] = ram_used
                self.data["ram_total_gb"] = ram_total
                self.data["ticks"] += 1
                if nv is not None:
                    self.data["gpu_name"] = nv[0]
                    self.data["gpu_util"] = nv[1]
                    self.data["vram_used_gb"] = nv[2]
                    self.data["vram_total_gb"] = nv[3]
                    self.data["gpu_temp"] = nv[4]
                if fps_val is not None:
                    self.data["fps"] = fps_val
                    self.data["ms"] = ms_val
                    self.data["draws"] = draws_val
                    self.data["history_ms"].append(ms_val)
                elif not game_pids and not game_open:
                    self.data["fps"] = 0.0
                    self.data["ms"] = 0.0
                    self.data["draws"] = 0

            dt = time.monotonic() - t0
            self.stop_event.wait(max(0.05, 0.25 - dt))

    def snapshot(self):
        with self.lock:
            snap = dict(self.data)
            snap["history_ms"] = list(self.data["history_ms"])
            return snap

    def stop(self):
        self.stop_event.set()


class ScreenRecorder:
    """Real-time hardware H.264 MP4 recorder targeting the game client window (with overlay)."""

    def __init__(self, target_fps=30):
        self.target_fps = target_fps
        self.recording = False
        self.start_time = 0.0
        self.last_saved = None
        self.last_error = None
        self._stop_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def toggle(self, get_rect_fn):
        with self._lock:
            if self.recording:
                self._stop_locked()
                return False
            self._start_locked(get_rect_fn)
            return True

    def _start_locked(self, get_rect_fn):
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = CLIPS_DIR / f"VIce_clip_{stamp}.mp4"
        self.last_error = None
        self._stop_evt.clear()
        self.recording = True
        self.start_time = time.monotonic()
        self._thread = threading.Thread(
            target=self._record_loop,
            args=(out_path, get_rect_fn),
            daemon=True,
        )
        self._thread.start()

    def _stop_locked(self):
        self._stop_evt.set()
        t = self._thread
        self.recording = False
        if t is not None and t.is_alive():
            t.join(timeout=4.0)
        self._thread = None

    def stop(self):
        with self._lock:
            if self.recording:
                self._stop_locked()

    def elapsed(self):
        if not self.recording:
            return 0.0
        return max(0.0, time.monotonic() - self.start_time)

    def _record_loop(self, out_path: Path, get_rect_fn):
        writer = None
        frames_written = 0
        try:
            with mss.mss() as sct:
                rect = get_rect_fn()
                if rect is None:
                    mon = sct.monitors[1]
                    rx, ry, rw, rh = mon["left"], mon["top"], mon["width"], mon["height"]
                else:
                    rx, ry, rw, rh = rect

                rw = max(320, rw & ~1)
                rh = max(180, rh & ~1)

                writer = cv2.VideoWriter(
                    str(out_path),
                    cv2.CAP_MSMF,
                    cv2.VideoWriter_fourcc(*"H264"),
                    float(self.target_fps),
                    (rw, rh),
                )
                if not writer.isOpened():
                    writer.release()
                    writer = cv2.VideoWriter(
                        str(out_path),
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        float(self.target_fps),
                        (rw, rh),
                    )
                if not writer.isOpened():
                    self.last_error = "Codec vidéo indisponible"
                    self.recording = False
                    return

                frame_period = 1.0 / float(self.target_fps)
                next_due = time.perf_counter()

                while not self._stop_evt.is_set():
                    cur = get_rect_fn()
                    if cur is not None:
                        rx, ry = cur[0], cur[1]
                    region = {"left": int(rx), "top": int(ry), "width": int(rw), "height": int(rh)}
                    raw = np.asarray(sct.grab(region))
                    bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                    if bgr.shape[1] != rw or bgr.shape[0] != rh:
                        bgr = cv2.resize(bgr, (rw, rh), interpolation=cv2.INTER_AREA)

                    now = time.perf_counter()
                    repeats = 1
                    if now > next_due + frame_period:
                        repeats = min(4, int((now - next_due) / frame_period) + 1)
                    for _ in range(repeats):
                        writer.write(bgr)
                        frames_written += 1
                        next_due += frame_period

                    sleep_for = next_due - time.perf_counter()
                    if sleep_for > 0.002:
                        self._stop_evt.wait(sleep_for)
        except Exception as e:
            self.last_error = str(e)
        finally:
            if writer is not None:
                writer.release()
            self.recording = False
            if frames_written > 0 and out_path.is_file():
                self.last_saved = out_path


class ViceOverlayApp:
    WIDTH = 292
    HEIGHT = 228
    TRANS_KEY = "#010203"

    def __init__(self):
        self.telemetry = TelemetryCollector()
        self.recorder = ScreenRecorder(target_fps=30)
        self.visible = True
        self.toast_text = ""
        self.toast_until = 0.0
        self._last_f9 = False
        self._last_f10 = False
        self._last_f11 = False
        self._last_pos = None
        self._hwnd = None

        self.root = tk.Tk()
        self.root.title("VIce Clip Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg=self.TRANS_KEY)
        try:
            self.root.wm_attributes("-transparentcolor", self.TRANS_KEY)
        except tk.TclError:
            pass
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+18+18")

        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=self.TRANS_KEY,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.root.update_idletasks()
        self._apply_overlay_style()

        self.root.after(45, self._tick_hotkeys)
        self.root.after(120, self._tick_render)

    def _resolve_hwnd(self):
        wid = wt.HWND(self.root.winfo_id())
        parent = _user32.GetParent(wid)
        return parent if parent else wid

    def _apply_overlay_style(self):
        hwnd = self._resolve_hwnd()
        self._hwnd = hwnd
        ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        _user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def _get_capture_rect(self):
        return self.telemetry.snapshot()["game_rect"]

    def _tick_hotkeys(self):
        f9 = (_user32.GetAsyncKeyState(VK_F9) & 0x8000) != 0
        f10 = (_user32.GetAsyncKeyState(VK_F10) & 0x8000) != 0
        f11 = (_user32.GetAsyncKeyState(VK_F11) & 0x8000) != 0

        if f9 and not self._last_f9:
            started = self.recorder.toggle(self._get_capture_rect)
            if started:
                self.toast_text = "REC démarré (H.264)"
                self.toast_until = time.monotonic() + 2.0
            else:
                saved = self.recorder.last_saved
                if saved:
                    self.toast_text = f"Sauvé : {saved.name}"
                    self.toast_until = time.monotonic() + 5.0
                elif self.recorder.last_error:
                    self.toast_text = f"Erreur : {self.recorder.last_error[:28]}"
                    self.toast_until = time.monotonic() + 5.0

        if f10 and not self._last_f10:
            self.visible = not self.visible
            if self.visible:
                self.root.deiconify()
                self._apply_overlay_style()
            else:
                self.root.withdraw()

        if f11 and not self._last_f11:
            self._quit()
            return

        self._last_f9, self._last_f10, self._last_f11 = f9, f10, f11
        self.root.after(40, self._tick_hotkeys)

    def _rounded_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _bar(self, x, y, w, h, ratio, color):
        ratio = max(0.0, min(1.0, ratio))
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#1E1B2E", outline="")
        if ratio > 0.01:
            self.canvas.create_rectangle(x, y, x + int(w * ratio), y + h, fill=color, outline="")

    def _tick_render(self):
        snap = self.telemetry.snapshot()

        # Snap cleanly to top-left inside the game client area and keep topmost above D3D12 window.
        rect = snap["game_rect"]
        if rect is not None:
            tx, ty = int(rect[0]) + 16, int(rect[1]) + 16
        else:
            tx, ty = 18, 18

        hwnd = self._hwnd or self._resolve_hwnd()
        if self._last_pos != (tx, ty):
            self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{tx}+{ty}")
            self._last_pos = (tx, ty)
        if self.visible and hwnd:
            _user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                tx, ty, self.WIDTH, self.HEIGHT,
                SWP_NOACTIVATE,
            )

        if self.visible:
            c = self.canvas
            c.delete("all")
            W, H = self.WIDTH, self.HEIGHT

            self._rounded_rect(2, 2, W - 2, H - 2, 14, fill="#0D0B16", outline="#3B2B5C", width=1.5)

            seg_colors = ["#8B5CF6", "#A855F7", "#D946EF", "#EC4899", "#F472B6", "#FB923C"]
            sx0 = 14
            seg_w = (W - 28) / len(seg_colors)
            for idx, col in enumerate(seg_colors):
                c.create_rectangle(
                    sx0 + idx * seg_w, 3, sx0 + (idx + 1) * seg_w + 1, 6,
                    fill=col, outline="",
                )

            c.create_text(16, 22, text="VI", anchor="w", fill="#C084FC", font=("Segoe UI Black", 13, "bold"))
            c.create_text(35, 22, text="ce", anchor="w", fill="#F8FAFC", font=("Segoe UI Semibold", 13, "bold"))
            c.create_text(60, 23, text="LIVE", anchor="w", fill="#8E86A8", font=("Segoe UI Semibold", 8))

            pulse = (snap["ticks"] % 2) == 0
            if self.recorder.recording:
                el = int(self.recorder.elapsed())
                mm, ss = divmod(el, 60)
                dot_col = "#EF4444" if pulse else "#7F1D1D"
                c.create_oval(W - 86, 17, W - 77, 26, fill=dot_col, outline="")
                c.create_text(
                    W - 14, 22, text=f"REC {mm:02d}:{ss:02d}", anchor="e",
                    fill="#FCA5A5", font=("Consolas", 9, "bold"),
                )
            else:
                if snap["game_open"]:
                    status_col = "#34D399" if pulse else "#10B981"
                    status_txt = "IN-GAME"
                elif snap["proc_running"]:
                    status_col = "#F59E0B" if pulse else "#B45309"
                    status_txt = "CHARGEMENT"
                else:
                    status_col = "#64748B"
                    status_txt = "EN ATTENTE"
                c.create_oval(W - 88, 18, W - 81, 25, fill=status_col, outline="")
                c.create_text(W - 14, 22, text=status_txt, anchor="e", fill="#CBD5E1", font=("Segoe UI Semibold", 8))

            c.create_line(14, 36, W - 14, 36, fill="#221D36")

            fps = snap["fps"]
            ms = snap["ms"]
            fps_col = "#34D399" if fps >= 24 else ("#FBBF24" if fps >= 12 else "#F472B6")
            fps_str = f"{fps:.1f}" if fps > 0 else "--"
            c.create_text(16, 57, text=fps_str, anchor="w", fill=fps_col, font=("Consolas", 20, "bold"))
            c.create_text(88, 51, text="FPS", anchor="w", fill="#E2E8F0", font=("Segoe UI Bold", 9, "bold"))
            ms_str = f"{ms:.1f} ms" if ms > 0 else "-- ms"
            c.create_text(88, 65, text=ms_str, anchor="w", fill="#94A3B8", font=("Consolas", 8))

            if snap["smooth_motion"] and fps > 0:
                c.create_text(
                    W - 14, 49, text=f"~{fps * 2:.0f} FPS SM", anchor="e",
                    fill="#C084FC", font=("Consolas", 9, "bold"),
                )
            elif snap["draws"] > 0:
                c.create_text(
                    W - 14, 49, text=f"{snap['draws']} draws", anchor="e",
                    fill="#94A3B8", font=("Consolas", 8),
                )

            hist = snap["history_ms"]
            gx0, gy0, gw, gh = 152, 56, W - 166, 22
            c.create_rectangle(gx0, gy0, gx0 + gw, gy0 + gh, fill="#151225", outline="#241E3A")
            peak = max(100.0, max(hist) if hist else 100.0)
            pts = []
            n = len(hist)
            for i, v in enumerate(hist):
                px = gx0 + 2 + (gw - 4) * (i / max(1, n - 1))
                py = gy0 + gh - 2 - (gh - 4) * min(1.0, v / peak)
                pts.extend((px, py))
            if len(pts) >= 4:
                c.create_line(*pts, fill="#EC4899", width=1.6, smooth=True)

            rows = [
                ("GPU", f"{snap['gpu_util']:.0f}%  {snap['gpu_temp']}°C", snap["gpu_util"] / 100.0, "#A855F7"),
                ("VRAM", f"{snap['vram_used_gb']:.1f} / {snap['vram_total_gb']:.1f} Go", snap["vram_used_gb"] / max(0.1, snap["vram_total_gb"]), "#EC4899"),
                ("CPU", f"{snap['cpu_util']:.0f}%", snap["cpu_util"] / 100.0, "#38BDF8"),
                ("RAM", f"{snap['ram_used_gb']:.1f} / {snap['ram_total_gb']:.1f} Go", snap["ram_used_gb"] / max(0.1, snap["ram_total_gb"]), "#FB923C"),
            ]
            y = 94
            for label, val, ratio, col in rows:
                c.create_text(16, y, text=label, anchor="w", fill="#A59EC0", font=("Segoe UI Semibold", 8, "bold"))
                c.create_text(W - 16, y, text=val, anchor="e", fill="#F1F5F9", font=("Consolas", 8, "bold"))
                self._bar(16, y + 9, W - 32, 5, ratio, col)
                y += 26

            now = time.monotonic()
            if now < self.toast_until and self.toast_text:
                c.create_text(W // 2, H - 14, text=self.toast_text, anchor="center", fill="#34D399", font=("Segoe UI Semibold", 8))
            elif self.recorder.recording:
                c.create_text(
                    W // 2, H - 14,
                    text=f"{snap['gpu_name']}  •  i7-13700HX",
                    anchor="center", fill="#7C749A", font=("Segoe UI", 8),
                )
            else:
                c.create_text(
                    W // 2, H - 14,
                    text="F9 Record MP4  •  F10 Masquer  •  F11 Quitter",
                    anchor="center", fill="#7C749A", font=("Segoe UI", 7.5),
                )
            c.update_idletasks()

        self.root.after(250, self._tick_render)

    def _quit(self):
        self.recorder.stop()
        self.telemetry.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.recorder.stop()
            self.telemetry.stop()


if __name__ == "__main__":
    ViceOverlayApp().run()
