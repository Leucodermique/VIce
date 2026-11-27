r"""VIce Pro Gameplay Auditor — Live Telemetry, Flash Detector, FPS Drop & Log Correlator.

Conçu pour HP OMEN 16 (RTX 4070 Laptop 8 Go VRAM / 16 Go RAM / Natif + Smooth Motion).
Zéro impact (<1% CPU, ring-buffer léger en RAM). Écrit ses sessions dans:
  %LOCALAPPDATA%\VIce\audit_sessions\<YYYY-MM-DD_HH-MM-SS>\

Déclencheurs automatiques pendant le gameplay :
  - FLASH_DETECTED : détection visuelle A -> B -> A ou saut brutal de luminance/zones noires
                     (sauvegarde une planche 3 volets Avant / Flash / Carte Diff rouge + HUD stats)
  - FPS_DROP       : chute >= 25% vs moyenne récente, FPS < seuil, ou frame >= 115 ms
  - LOG_CRITICAL   : slots dynamiques saturés, draws droppés, erreurs/WARN critiques, pics MakeResident/sweep
  - BASELINE       : capture de référence périodique en gameplay actif
  - F8             : capture manuelle instantanée (MANUAL_SNAP)
  - F7             : rafale manuelle de 5 frames espacées de 120 ms (MANUAL_BURST)

Modes :
  python scripts/vice_pro_audit.py                  # Lance le monitor live (attend le jeu)
  python scripts/vice_pro_audit.py --until-exit     # S'arrête et génère le rapport quand le jeu ferme
  python scripts/vice_pro_audit.py --audit-only     # Audit statique complet du dernier play.log
  python scripts/vice_pro_audit.py --self-test      # Vérifie NVML, mss, OpenCV/Pillow et le pipeline
"""
from __future__ import annotations

import argparse
import collections
import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import signal
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import mss
import numpy as np
import psutil

try:
    import dxcam  # type: ignore
except Exception:
    dxcam = None

# ---- Chemins par défaut ----
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
PLAY_LOG = LOCALAPPDATA / "VIce" / "launcher" / "play.log"
SESSIONS_ROOT = LOCALAPPDATA / "VIce" / "audit_sessions"
GAME_EXES = {"victory.exe", "victory_sm.exe", "victory-diag.exe", "victory-debug.exe"}

# Hotkeys sans conflit avec F1 (menu), F3 (stats), F9/F10/F11 (VIce Clip Overlay)
VK_F7 = 0x76
VK_F8 = 0x77

# ---- Bindings Win32 64-bit stricts ----
_user32 = ctypes.WinDLL("user32", use_last_error=True)
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
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wt.HWND
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # Per-Monitor DPI aware
except Exception:
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass

# ---- Regex play.log ----
FPS_RE = re.compile(
    r"(?P<fps>\d+(?:\.\d+)?)\s+fps,\s+(?P<ms>\d+(?:\.\d+)?)\s+ms/frame\s+"
    r"\((?P<prep>\d+(?:\.\d+)?)\s+preparing:\s+copies\s+(?P<copy_ms>\d+(?:\.\d+)?)\s+ms\s+(?P<copy_mb>\d+(?:\.\d+)?)\s+MB,\s+"
    r"textures\s+(?P<tex_ms>\d+(?:\.\d+)?)\s+ms\s+(?P<tex_mb>\d+(?:\.\d+)?)\s+MB;\s+"
    r"(?P<gpu>\d+(?:\.\d+)?)\s+on the GPU;\s+"
    r"(?P<back_ms>\d+(?:\.\d+)?)\s+copying back\s+(?P<back_mb>\d+(?:\.\d+)?)\s+MB\),\s+"
    r"(?P<draws>\d+)\s+draws/frame,\s+dropped\s+(?P<dropped>\d+).*?"
    r"heap buffers\s+\d+(?:\.\d+)?\s+passes\s+(?P<heap_ms>\d+(?:\.\d+)?)\s+ms.*?"
    r"textures picked by index:\s+(?P<up_n>\d+)\s+uploaded so far\s+\((?P<up_mb>\d+)\s+MB\),\s+"
    r"(?P<waiting>\d+)\s+waiting,\s+(?P<unmapped>\d+)\s+passed over unmapped"
)
QUEUE_RE = re.compile(
    r"textures picked by index, the queue:\s+(\d+)\s+slots,\s+(\d+)\s+of them due\s+\(the oldest written\s+(\d+)\s+frames ago\)"
)
SWEEP_RE = re.compile(
    r"(\d+)\s+looks for textures to let go,\s+(\d+(?:\.\d+)?)\s+ms each,\s+the longest\s+(\d+(?:\.\d+)?)"
)
RES_RE = re.compile(
    r"new memory waited for.*?: (\d+) times,\s+(\d+(?:\.\d+)?)\s+ms,\s+the longest\s+(\d+(?:\.\d+)?)\s+ms"
)
MAP_RE = re.compile(
    r"new card memory waited for.*?: (\d+) times,\s+(\d+(?:\.\d+)?)\s+ms,\s+the longest\s+(\d+(?:\.\d+)?)\s+ms"
)
CARD_MEM_RE = re.compile(
    r"the card:\s+(\d+)\s+MB in use of a budget of\s+(\d+)\s+MB,\s+(\d+)\s+MB of the PC's memory shared"
)
CRITICAL_PATTERNS = [
    ("NO_FREE_DYN_SLOTS", re.compile(r"no free dynamic descriptor slots|found no free slots is dropped", re.I)),
    ("HEAP_CAPPED", re.compile(r"host view heap capped at", re.I)),
    ("TEX_CHANGED_SILENT", re.compile(r"a texture picked by index changed after its upload", re.I)),
    ("D3D12_ERROR", re.compile(r" ERROR |DXGI_ERROR|DEVICE_REMOVED|0x887A|0x8007000E", re.I)),
    ("PIPELINE_REFUSED", re.compile(r"host pipeline refused", re.I)),
]


# ---- NVML GPU Reader natif (~1 ms) ----
class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NvmlReader:
    def __init__(self) -> None:
        self.lib = None
        self.handle = ctypes.c_void_p()
        self.name = "GPU inconnu"
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
                    self.name = buf.value.decode("utf-8", errors="replace").strip() or self.name
        except Exception:
            self.lib = None

    def read(self) -> dict[str, Any]:
        if self.lib is None:
            return {"gpu_name": self.name, "gpu_pct": None, "vram_used_mb": None, "vram_total_mb": None, "gpu_temp_c": None}
        try:
            u = _NvmlUtil()
            m = _NvmlMem()
            temp = ctypes.c_uint(0)
            if (
                self.lib.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(u)) == 0
                and self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(m)) == 0
            ):
                self.lib.nvmlDeviceGetTemperature(self.handle, 0, ctypes.byref(temp))
                return {
                    "gpu_name": self.name,
                    "gpu_pct": int(u.gpu),
                    "vram_used_mb": round(m.used / (1024 * 1024)),
                    "vram_total_mb": round(m.total / (1024 * 1024)),
                    "gpu_temp_c": int(temp.value),
                }
        except Exception:
            pass
        return {"gpu_name": self.name, "gpu_pct": None, "vram_used_mb": None, "vram_total_mb": None, "gpu_temp_c": None}


def find_game_processes() -> dict[int, tuple[str, psutil.Process]]:
    found: dict[int, tuple[str, psutil.Process]] = {}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            nm = (p.info.get("name") or "").lower()
            if nm in GAME_EXES or nm.startswith("vice-"):
                found[p.info["pid"]] = (nm, p)
        except Exception:
            pass
    return found


def find_game_window(game_pids: dict[int, tuple[str, psutil.Process]]) -> tuple[int, str, tuple[int, int, int, int], str, int] | None:
    candidates = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        entry = game_pids.get(pid.value)
        exe = entry[0] if entry else ""

        length = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        tlow = title.lower()
        if not exe and "americas | victory" not in tlow and "| victory " not in tlow:
            return True

        crect = wt.RECT()
        pt = wt.POINT(0, 0)
        if _user32.GetClientRect(hwnd, ctypes.byref(crect)) and _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            w = crect.right - crect.left
            h = crect.bottom - crect.top
            if w >= 320 and h >= 200:
                candidates.append((w * h, hwnd, title, (pt.x, pt.y, w, h), exe or "victory.exe", pid.value))
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, hwnd, title, rect, exe, pid = candidates[0]
    return hwnd, title, rect, exe, pid


@dataclass
class FrameSnap:
    ts: float
    iso: str
    bgr: np.ndarray        # image complète ou redimensionnée <= 1280x720
    small_gray: np.ndarray # 320x180 float32 pour détection flash ultra-rapide
    luma_mean: float
    dark_ratio: float


@dataclass
class LogState:
    header: list[str] = field(default_factory=list)
    last_fps: dict[str, float] | None = None
    world_samples: list[dict[str, float]] = field(default_factory=list)
    recent_world_fps: collections.deque = field(default_factory=lambda: collections.deque(maxlen=10))
    last_queue: tuple[int, int, int] | None = None
    last_sweep: tuple[int, float, float] | None = None
    last_res: tuple[int, float, float] | None = None
    last_map: tuple[int, float, float] | None = None
    last_card_mem: tuple[int, int, int] | None = None
    errors: int = 0
    warnings: int = 0
    warn_counts: dict[str, int] = field(default_factory=dict)
    recent_lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=40))


class PlayLogTailer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos = 0
        self.mtime = 0.0
        self.state = LogState()
        self._last_res_max = 0.0
        self._last_sweep_max = 0.0

    def reset_to_start(self) -> None:
        self.pos = 0
        self.state = LogState()
        self._last_res_max = 0.0
        self._last_sweep_max = 0.0

    def poll(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Lit les nouvelles lignes de play.log et renvoie la liste des alertes déclenchées."""
        alerts: list[tuple[str, str, dict[str, Any]]] = []
        if not self.path.is_file():
            return alerts
        try:
            st = self.path.stat()
        except OSError:
            return alerts

        # Nouveau lancement du jeu (fichier tronqué)
        if st.st_size < self.pos:
            self.reset_to_start()

        if st.st_size == self.pos:
            return alerts

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
        except OSError:
            return alerts

        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            if len(self.state.header) < 8 and not line.startswith("202"):
                self.state.header.append(line)
            self.state.recent_lines.append(line)

            if " ERROR " in line:
                self.state.errors += 1
            elif " WARN " in line:
                self.state.warnings += 1
                key = re.sub(r"^\S+\s+WARN\s+", "", line)
                key = re.sub(r"=0x[0-9a-fA-F]+", "=#", key)
                key = re.sub(r"=\d+", "=#", key)[:120]
                self.state.warn_counts[key] = self.state.warn_counts.get(key, 0) + 1

            for tag, pat in CRITICAL_PATTERNS:
                if pat.search(line):
                    alerts.append((f"LOG_{tag}", line[:220], {"line": line}))
                    break

            m = FPS_RE.search(line)
            if m:
                d = {k: float(v) for k, v in m.groupdict().items()}
                self.state.last_fps = d
                if d["draws"] >= 500:
                    avg_before = (
                        statistics.mean(self.state.recent_world_fps)
                        if len(self.state.recent_world_fps) >= 3
                        else None
                    )
                    self.state.world_samples.append(d)
                    self.state.recent_world_fps.append(d["fps"])
                    alerts.append(("FPS_SAMPLE", f"{d['fps']:.1f} fps", {"sample": d, "avg_before": avg_before}))

            if m := QUEUE_RE.search(line):
                self.state.last_queue = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if m := SWEEP_RE.search(line):
                cnt, avg_ms, max_ms = int(m.group(1)), float(m.group(2)), float(m.group(3))
                self.state.last_sweep = (cnt, avg_ms, max_ms)
                if max_ms > self._last_sweep_max and max_ms >= 40.0:
                    self._last_sweep_max = max_ms
                    alerts.append(("LOG_SWEEP_SPIKE", f"Texture sweep spike {max_ms:.1f} ms (avg {avg_ms:.2f} ms)", {"max_ms": max_ms}))
            if m := RES_RE.search(line):
                cnt, tot_ms, max_ms = int(m.group(1)), float(m.group(2)), float(m.group(3))
                self.state.last_res = (cnt, tot_ms, max_ms)
                if max_ms > self._last_res_max and max_ms >= 300.0:
                    self._last_res_max = max_ms
                    alerts.append(("LOG_MAKERESIDENT_SPIKE", f"MakeResident spike {max_ms:.1f} ms ({cnt} calls)", {"max_ms": max_ms}))
            if m := MAP_RE.search(line):
                self.state.last_map = (int(m.group(1)), float(m.group(2)), float(m.group(3)))
            if m := CARD_MEM_RE.search(line):
                self.state.last_card_mem = (int(m.group(1)), int(m.group(2)), int(m.group(3)))

        return alerts


def resize_max(bgr: np.ndarray, max_w: int = 1280) -> np.ndarray:
    h, w = bgr.shape[:2]
    if w <= max_w:
        return bgr
    scale = max_w / float(w)
    return cv2.resize(bgr, (max_w, int(round(h * scale))), interpolation=cv2.INTER_AREA)


def draw_hud_banner(bgr: np.ndarray, title: str, lines: list[str]) -> np.ndarray:
    """Ajoute un bandeau noir semi-opaque en haut de l'image avec les stats exactes."""
    out = bgr.copy()
    h, w = out.shape[:2]
    bar_h = 26 + 20 * len(lines)
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (w, min(bar_h, h)), (12, 14, 20), -1)
    cv2.addWeighted(overlay, 0.82, out, 0.18, 0, out)
    cv2.putText(out, title, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (90, 220, 255), 1, cv2.LINE_AA)
    y = 40
    for ln in lines:
        cv2.putText(out, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (235, 240, 245), 1, cv2.LINE_AA)
        y += 20
    return out


def build_flash_strip(
    before: FrameSnap,
    flash: FrameSnap,
    after: FrameSnap | None,
    banner_title: str,
    banner_lines: list[str],
) -> np.ndarray:
    """Assemble Avant | Flash | Diff rouge (ou Après) en une planche de diagnostic ultra-lisible."""
    tw, th = 640, 360
    img_b = cv2.resize(before.bgr, (tw, th), interpolation=cv2.INTER_AREA)
    img_f = cv2.resize(flash.bgr, (tw, th), interpolation=cv2.INTER_AREA)
    diff = cv2.absdiff(img_b, img_f)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    # Met en évidence en rouge vif les zones qui ont flashé (diff > 22)
    heat = img_f.copy()
    mask = diff_gray > 22
    heat[mask] = (heat[mask].astype(np.float32) * 0.35 + np.array([0, 0, 255], dtype=np.float32) * 0.65).astype(np.uint8)

    def label(im: np.ndarray, txt: str, col: tuple[int, int, int]) -> np.ndarray:
        c = im.copy()
        cv2.rectangle(c, (0, th - 26), (tw, th), (10, 10, 14), -1)
        cv2.putText(c, txt, (8, th - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.52, col, 1, cv2.LINE_AA)
        return c

    p1 = label(img_b, f"AVANT ({before.iso}) luma={before.luma_mean:.1f}", (180, 230, 180))
    p2 = label(img_f, f"FLASH ({flash.iso}) luma={flash.luma_mean:.1f}", (90, 150, 255))
    p3 = label(heat, f"ZONES FLASHEES (>22) px={mask.mean()*100:.1f}%", (80, 80, 255))

    if after is not None:
        img_a = cv2.resize(after.bgr, (tw, th), interpolation=cv2.INTER_AREA)
        p4 = label(img_a, f"APRES ({after.iso}) luma={after.luma_mean:.1f}", (180, 230, 255))
        grid = np.vstack([np.hstack([p1, p2]), np.hstack([p3, p4])])
    else:
        grid = np.hstack([p1, p2, p3])

    # Bandeau stats au-dessus sans masquer l'image
    bar_h = 28 + 20 * len(banner_lines)
    bar = np.full((bar_h, grid.shape[1], 3), (14, 16, 24), dtype=np.uint8)
    cv2.putText(bar, banner_title, (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (80, 225, 255), 1, cv2.LINE_AA)
    y = 42
    for ln in banner_lines:
        cv2.putText(bar, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (235, 240, 245), 1, cv2.LINE_AA)
        y += 20
    return np.vstack([bar, grid])


class ProAuditor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.nvml = NvmlReader()
        self.tailer = PlayLogTailer(Path(args.log))
        self.stop_requested = False

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = SESSIONS_ROOT / stamp
        self.shots_dir = self.session_dir / "shots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        (SESSIONS_ROOT / "latest.txt").write_text(str(self.session_dir), encoding="utf-8")

        self.events_path = self.session_dir / "events.jsonl"
        self.timeline_path = self.session_dir / "timeline.csv"
        self.report_path = self.session_dir / "report.md"

        self.events: list[dict[str, Any]] = []
        self.frame_ring: collections.deque[FrameSnap] = collections.deque(maxlen=6)
        self.cooldowns: dict[str, float] = {}
        self.shot_counter = 0
        self.flash_count = 0
        self.fps_drop_count = 0
        self.saw_game = False
        self.last_baseline_ts = 0.0
        self.last_timeline_ts = 0.0
        self._f7_down = False
        self._f8_down = False
        self._pending_flash: dict[str, Any] | None = None

        with self.timeline_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "iso", "fps", "frame_ms", "prep_ms", "gpu_ms", "back_ms",
                "draws", "dropped", "tex_up", "tex_wait", "tex_unmapped",
                "gpu_pct", "vram_used_mb", "vram_total_mb", "gpu_temp_c",
                "cpu_pct", "ram_free_mb", "game_ram_mb", "luma_mean", "flash_mad",
            ])

    def _can_fire(self, key: str, cooldown_s: float) -> bool:
        now = time.monotonic()
        if now - self.cooldowns.get(key, 0.0) < cooldown_s:
            return False
        self.cooldowns[key] = now
        return True

    def _collect_hw(self, game_proc: psutil.Process | None) -> dict[str, Any]:
        gpu = self.nvml.read()
        vm = psutil.virtual_memory()
        game_ram_mb = None
        if game_proc is not None:
            try:
                game_ram_mb = round(game_proc.memory_info().rss / (1024 * 1024))
            except Exception:
                pass
        return {
            **gpu,
            "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
            "ram_used_mb": round(vm.used / (1024 * 1024)),
            "ram_free_mb": round(vm.available / (1024 * 1024)),
            "ram_total_mb": round(vm.total / (1024 * 1024)),
            "game_ram_mb": game_ram_mb,
        }

    def _banner_lines(self, hw: dict[str, Any], detail: str) -> list[str]:
        lf = self.tailer.state.last_fps
        if lf:
            l1 = (
                f"FPS: {lf['fps']:.1f} ({lf['ms']:.1f} ms) | Prep: {lf['prep']:.1f} ms "
                f"(Heap {lf['heap_ms']:.1f}, Copy {lf['copy_ms']:.1f}) | GPU: {lf['gpu']:.1f} ms | "
                f"Back: {lf['back_ms']:.1f} ms | Draws: {int(lf['draws'])} (drop {int(lf['dropped'])})"
            )
            l2 = (
                f"Textures: {int(lf['up_n'])} up ({int(lf['up_mb'])} MB) | Wait: {int(lf['waiting'])} | "
                f"Unmap: {int(lf['unmapped'])}"
            )
        else:
            l1 = "play.log: en attente d'echantillon FPS..."
            l2 = "Textures: n/a"

        q = self.tailer.state.last_queue
        if q:
            l2 += f" | Queue: {q[1]} due (oldest {q[2]}f)"

        l3 = (
            f"GPU: {hw.get('gpu_pct')}% {hw.get('gpu_temp_c')}C | "
            f"VRAM: {hw.get('vram_used_mb')}/{hw.get('vram_total_mb')} MB | "
            f"CPU: {hw.get('cpu_pct')}% | RAM libre: {hw.get('ram_free_mb')} MB "
            f"(Jeu: {hw.get('game_ram_mb')} MB)"
        )
        return [l1, l2, l3, f"Detail: {detail[:130]}"]

    def _record_event(
        self,
        kind: str,
        summary: str,
        hw: dict[str, Any],
        shot_path: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        iso = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        ev = {
            "time": iso,
            "kind": kind,
            "summary": summary,
            "shot": str(shot_path) if shot_path else None,
            "fps": self.tailer.state.last_fps,
            "queue": self.tailer.state.last_queue,
            "sweep": self.tailer.state.last_sweep,
            "make_resident": self.tailer.state.last_res,
            "hw": hw,
            "log_tail": list(self.tailer.state.recent_lines)[-8:],
            "extra": extra or {},
        }
        self.events.append(ev)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        shot_txt = f" -> {shot_path.name}" if shot_path else ""
        print(f"[{iso}] [{kind}] {summary}{shot_txt}", flush=True)

    def _save_single_shot(self, kind: str, summary: str, frame: FrameSnap, hw: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        self.shot_counter += 1
        fname = f"{self.shot_counter:03d}_{kind.lower()}_{datetime.now().strftime('%H%M%S_%f')[:-3]}.png"
        out_path = self.shots_dir / fname
        title = f"VIce Pro Audit | {kind} | {frame.iso}"
        annotated = draw_hud_banner(frame.bgr, title, self._banner_lines(hw, summary))
        cv2.imwrite(str(out_path), annotated, [cv2.IMWRITE_PNG_COMPRESSION, 5])
        self._record_event(kind, summary, hw, out_path, extra)

    def _grab_frame(self, sct: mss.mss, rect: tuple[int, int, int, int], dx_cam: Any = None) -> FrameSnap | None:
        x, y, w, h = rect
        if w < 320 or h < 200:
            return None
        bgr = None
        if dx_cam is not None:
            try:
                # DXGI Desktop Duplication : capture D3D12 / DirectFlip / Smooth Motion sans écran noir
                mon = sct.monitors[1]
                rx1 = max(0, int(x - mon["left"]))
                ry1 = max(0, int(y - mon["top"]))
                rx2 = min(mon["width"], rx1 + int(w))
                ry2 = min(mon["height"], ry1 + int(h))
                region = (rx1, ry1, rx2, ry2) if (rx2 - rx1 >= 320 and ry2 - ry1 >= 200) else None
                raw_dx = dx_cam.grab(region=region) if region else dx_cam.grab()
                if raw_dx is not None and raw_dx.size > 0:
                    bgr = resize_max(raw_dx, max_w=1280)
            except Exception:
                bgr = None
        if bgr is None:
            try:
                raw = sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
                bgr = resize_max(np.asarray(raw)[:, :, :3], max_w=1280)
            except Exception:
                return None
        small = cv2.resize(bgr, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        luma_mean = float(gray.mean())
        dark_ratio = float((gray < 14.0).mean())
        return FrameSnap(
            ts=time.monotonic(),
            iso=datetime.now().strftime("%H:%M:%S.%f")[:-3],
            bgr=bgr,
            small_gray=gray,
            luma_mean=luma_mean,
            dark_ratio=dark_ratio,
        )

    def _detect_flash(self, curr: FrameSnap) -> tuple[bool, str, float, FrameSnap | None, FrameSnap | None]:
        """Détecte un flash visuel (A->B->A, texture qui disparaît/revient, ou saut brutal de luma/noir)."""
        if len(self.frame_ring) < 2:
            return False, "", 0.0, None, None
        prev1 = self.frame_ring[-1]
        prev2 = self.frame_ring[-2]

        diff_01 = np.abs(curr.small_gray - prev1.small_gray)
        mad_01 = float(diff_01.mean())
        changed_px_01 = float((diff_01 > 28.0).mean())

        # Score A -> B -> A : prev2 et curr se ressemblent alors que prev1 s'en écarte fortement !
        diff_21 = np.abs(prev1.small_gray - prev2.small_gray)
        diff_20 = np.abs(curr.small_gray - prev2.small_gray)
        mad_21 = float(diff_21.mean())
        mad_20 = float(diff_20.mean())
        changed_px_21 = float((diff_21 > 28.0).mean())

        luma_jump = abs(curr.luma_mean - prev1.luma_mean)
        dark_jump = abs(curr.dark_ratio - prev1.dark_ratio)

        # Cas 1 : Flash classique A -> B -> A (texture/objet qui clignote sur 1 frame)
        if mad_21 >= self.args.flash_mad and changed_px_21 >= 0.06 and mad_20 <= mad_21 * 0.55:
            reason = (
                f"Clignotement A->B->A: MAD(avant,flash)={mad_21:.1f}, "
                f"MAD(avant,apres)={mad_20:.1f}, zones={changed_px_21*100:.1f}%"
            )
            return True, reason, mad_21, prev2, prev1

        # Cas 2 : Saut brutal de luminance ou blackout partiel (écran/textures noires soudaines)
        if luma_jump >= 24.0 or (dark_jump >= 0.18 and changed_px_01 >= 0.15):
            reason = (
                f"Saut luma/noir: dLuma={luma_jump:.1f} ({prev1.luma_mean:.0f}->{curr.luma_mean:.0f}), "
                f"dNoir={dark_jump*100:.1f}%, MAD={mad_01:.1f}"
            )
            return True, reason, mad_01, prev1, curr

        return False, "", mad_01, None, None

    def _capture_manual_burst(self, sct: mss.mss, rect: tuple[int, int, int, int], hw: dict[str, Any]) -> None:
        frames: list[FrameSnap] = []
        for _ in range(5):
            f = self._grab_frame(sct, rect)
            if f is not None:
                frames.append(f)
            time.sleep(0.12)
        if len(frames) < 2:
            return
        # Sauvegarde une planche comparative 2x2 des 4 premières frames + diff max
        tw, th = 640, 360
        tiles = []
        for idx, fr in enumerate(frames[:4]):
            im = cv2.resize(fr.bgr, (tw, th), interpolation=cv2.INTER_AREA)
            cv2.rectangle(im, (0, th - 24), (tw, th), (10, 10, 14), -1)
            cv2.putText(im, f"F{idx+1} {fr.iso} luma={fr.luma_mean:.1f}", (8, th - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 230, 255), 1, cv2.LINE_AA)
            tiles.append(im)
        while len(tiles) < 4:
            tiles.append(np.zeros((th, tw, 3), dtype=np.uint8))
        grid = np.vstack([np.hstack([tiles[0], tiles[1]]), np.hstack([tiles[2], tiles[3]])])
        lines = self._banner_lines(hw, f"Rafale manuelle F7 ({len(frames)} frames @ 120ms)")
        bar_h = 28 + 20 * len(lines)
        bar = np.full((bar_h, grid.shape[1], 3), (14, 16, 24), dtype=np.uint8)
        cv2.putText(bar, f"VIce Pro Audit | MANUAL_BURST (F7) | {frames[0].iso}", (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 225, 255), 1, cv2.LINE_AA)
        y = 42
        for ln in lines:
            cv2.putText(bar, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (235, 240, 245), 1, cv2.LINE_AA)
            y += 20
        sheet = np.vstack([bar, grid])
        self.shot_counter += 1
        out_path = self.shots_dir / f"{self.shot_counter:03d}_manual_burst_{datetime.now().strftime('%H%M%S')}.png"
        cv2.imwrite(str(out_path), sheet, [cv2.IMWRITE_PNG_COMPRESSION, 5])
        self._record_event("MANUAL_BURST", f"Rafale F7 ({len(frames)} frames @ 120ms)", hw, out_path)

    def write_report(self) -> Path:
        st = self.tailer.state
        lines: list[str] = [
            "# VIce Pro Audit Report",
            "",
            f"- **Session** : `{self.session_dir.name}`",
            f"- **Log source** : `{self.tailer.path}`",
            f"- **Dossier captures** : `{self.shots_dir}`",
            f"- **Événements enregistrés** : `{len(self.events)}` (Flashs auto : **{self.flash_count}**, Chutes FPS : **{self.fps_drop_count}**, Captures PNG : **{self.shot_counter}**)",
            "",
        ]
        if st.header:
            lines.append("## En-tête du build / flags (`play.log`)")
            lines.append("```text")
            lines.extend(st.header[:8])
            lines.append("```")
            lines.append("")

        lines.append("## Synthèse télémétrie Gameplay")
        lines.append(f"- **Erreurs** : `{st.errors}` | **Warnings** : `{st.warnings}` | **Échantillons monde (`draws >= 500`)** : `{len(st.world_samples)}`")
        if st.world_samples:
            fps_vals = sorted(x["fps"] for x in st.world_samples)
            p10 = fps_vals[max(0, int(len(fps_vals) * 0.10))]
            tail = st.world_samples[-12:]
            avg = lambda k: statistics.mean(x[k] for x in tail)
            last = tail[-1]
            lines.append(
                f"- **FPS monde** : moy `{statistics.mean(fps_vals):.1f}` | steady-state (12 derniers) `{avg('fps'):.1f}` | "
                f"p10 `{p10:.1f}` | min `{fps_vals[0]:.1f}` | max `{fps_vals[-1]:.1f}`"
            )
            lines.append(
                f"- **Frame breakdown (steady)** : `{avg('ms'):.1f} ms` — Prep `{avg('prep'):.1f} ms` "
                f"(copies `{avg('copy_ms'):.1f} ms` / `{avg('copy_mb'):.1f} MB`, heap `{avg('heap_ms'):.1f} ms`) | "
                f"GPU `{avg('gpu'):.1f} ms` | Back `{avg('back_ms'):.1f} ms` (`{avg('back_mb'):.1f} MB`) | "
                f"Draws `{avg('draws'):.0f}` (dropped `{avg('dropped'):.0f}`)"
            )
            lines.append(
                f"- **Textures** : `{int(last['up_n'])}` uploadées (`{int(last['up_mb'])} MB`) | "
                f"`waiting={int(last['waiting'])}` | `unmapped={int(last['unmapped'])}`"
            )
        if st.last_queue:
            lines.append(f"- **Queue textures** : `{st.last_queue[0]}` slots, `{st.last_queue[1]}` due (plus ancienne : `{st.last_queue[2]}` frames)")
        if st.last_sweep:
            lines.append(f"- **Texture sweep** : `{st.last_sweep[0]}` passes, moy `{st.last_sweep[1]} ms`, max `{st.last_sweep[2]} ms`")
        if st.last_res:
            lines.append(f"- **MakeResident waits** : `{st.last_res[0]}` appels, total `{st.last_res[1]} ms`, max `{st.last_res[2]} ms`")
        if st.last_map:
            lines.append(f"- **Mapped waits** : `{st.last_map[0]}` appels, total `{st.last_map[1]} ms`, max `{st.last_map[2]} ms`")
        if st.last_card_mem:
            lines.append(f"- **Mémoire D3D12 (fin)** : `{st.last_card_mem[0]} / {st.last_card_mem[1]} MB` VRAM, `{st.last_card_mem[2]} MB` RAM partagée")
        lines.append("")

        if st.warn_counts:
            lines.append("## Top Warnings")
            lines.append("| Occurrences | Warning |")
            lines.append("| ---: | :--- |")
            for k, v in sorted(st.warn_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]:
                lines.append(f"| {v} | `{k}` |")
            lines.append("")

        if self.events:
            lines.append("## Journal des captures & événements")
            lines.append("| Heure | Type | Détail | FPS / VRAM | Capture |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for ev in self.events:
                fps_s = f"{ev['fps']['fps']:.1f} fps" if ev.get("fps") else "n/a"
                vram_s = f"{ev['hw'].get('vram_used_mb')} MB" if ev.get("hw", {}).get("vram_used_mb") is not None else "n/a"
                shot_link = f"[{Path(ev['shot']).name}](file:///{Path(ev['shot']).as_posix()})" if ev.get("shot") else "-"
                summary_clean = ev["summary"].replace("|", "/")
                lines.append(f"| `{ev['time']}` | **{ev['kind']}** | {summary_clean} | `{fps_s}` / `{vram_s}` | {shot_link} |")
            lines.append("")

        self.report_path.write_text("\n".join(lines), encoding="utf-8")
        return self.report_path

    def run_live(self) -> int:
        print("=== VIce Pro Gameplay Auditor ===", flush=True)
        print(f"Session : {self.session_dir}", flush=True)
        print(f"Log     : {self.tailer.path}", flush=True)
        print(f"GPU     : {self.nvml.name}", flush=True)
        print("Hotkeys : [F8] Capture instantanee | [F7] Rafale Flash 5x120ms | [Ctrl+C] Stop + Rapport", flush=True)
        print("En attente de la fenetre du jeu (victory.exe / victory_sm.exe)...", flush=True)

        psutil.cpu_percent(interval=None)
        dx_cam = None
        if dxcam is not None:
            try:
                dx_cam = dxcam.create(output_color="BGR")
                print("Capture : DXGI Desktop Duplication (dxcam) actif !", flush=True)
            except Exception:
                dx_cam = None
                print("Capture : fallback MSS (lance VIce Pro Audit.cmd depuis ton bureau Windows pour DXGI).", flush=True)
        try:
          with mss.mss() as sct:
            # Test rapide anti-shell-isolé
            probe = self._grab_frame(sct, (sct.monitors[1]["left"], sct.monitors[1]["top"], sct.monitors[1]["width"], sct.monitors[1]["height"]), dx_cam=dx_cam)
            if probe is not None and probe.bgr.max() == 0:
                print("ATTENTION : l'ecran retourne 0 (shell non-interactif). Double-clique sur 'VIce Pro Audit.cmd' depuis Windows !", flush=True)
            missing_since = None
            while not self.stop_requested:
                t0 = time.monotonic()
                procs = find_game_processes()
                win = find_game_window(procs)

                # Fallback fiable : uniquement si un vrai processus victory*.exe tourne
                # (ne jamais déclencher saw_game uniquement sur un play.log récent d'une
                # session précédente ou d'un démarrage incomplet).
                if win is None and procs:
                    mon = sct.monitors[1]
                    first_pid = next(iter(procs.keys()), 0)
                    first_exe = procs[first_pid][0] if first_pid else "victory"
                    win = (0, "VIce (fallback moniteur)", (mon["left"], mon["top"], mon["width"], mon["height"]), first_exe, first_pid)

                if win is None:
                    if self.saw_game and self.args.until_exit:
                        if missing_since is None:
                            missing_since = time.monotonic()
                        elif time.monotonic() - missing_since >= 8.0:
                            print("Jeu ferme (confirme 8s) -> generation du rapport final...", flush=True)
                            break
                    time.sleep(0.5)
                    continue

                missing_since = None
                if not self.saw_game:
                    self.saw_game = True
                    print(f"Jeu detecte : {win[3]} ({win[2][2]}x{win[2][3]}) — audit actif !", flush=True)

                hwnd, title, rect, exe, pid = win
                game_proc = procs[pid][1] if pid in procs else None

                # 1. Lire play.log en temps réel
                alerts = self.tailer.poll()
                hw = self._collect_hw(game_proc)

                # 2. Capturer la frame du jeu
                frame = self._grab_frame(sct, rect, dx_cam=dx_cam)
                flash_mad = 0.0
                if frame is not None:
                    # Détection automatique de FLASH (uniquement si la fenêtre du jeu est au premier plan -> évite les faux positifs Alt+Tab)
                    fg = _user32.GetForegroundWindow()
                    game_focused = (hwnd == 0) or (fg == hwnd)
                    is_world = bool(self.tailer.state.last_fps and self.tailer.state.last_fps["draws"] >= 500)
                    if game_focused and (is_world or self.args.detect_in_menus):
                        is_flash, reason, flash_mad, f_before, f_flash = self._detect_flash(frame)
                        if is_flash and f_before is not None and f_flash is not None and self._can_fire("FLASH", self.args.flash_cooldown):
                            self.flash_count += 1
                            self.shot_counter += 1
                            fname = f"{self.shot_counter:03d}_flash_strip_{datetime.now().strftime('%H%M%S_%f')[:-3]}.png"
                            out_path = self.shots_dir / fname
                            after_snap = frame if f_flash is not frame else None
                            strip = build_flash_strip(
                                f_before,
                                f_flash,
                                after_snap,
                                f"VIce Pro Audit | FLASH_DETECTED #{self.flash_count} | {frame.iso}",
                                self._banner_lines(hw, reason),
                            )
                            cv2.imwrite(str(out_path), strip, [cv2.IMWRITE_PNG_COMPRESSION, 5])
                            self._record_event("FLASH_DETECTED", reason, hw, out_path, {"flash_mad": round(flash_mad, 2)})

                    self.frame_ring.append(frame)

                    # 3. Traiter les alertes play.log (FPS_DROP + critiques)
                    for kind, msg, payload in alerts:
                        if kind == "FPS_SAMPLE":
                            s = payload["sample"]
                            avg_before = payload["avg_before"]
                            drop_by_pct = avg_before is not None and avg_before >= 10.0 and s["fps"] <= avg_before * 0.75
                            drop_abs = s["fps"] <= self.args.fps_threshold or s["ms"] >= 115.0
                            if (drop_by_pct or drop_abs) and self._can_fire("FPS_DROP", self.args.fps_cooldown):
                                self.fps_drop_count += 1
                                reason = (
                                    f"Chute FPS: {s['fps']:.1f} fps ({s['ms']:.0f} ms) "
                                    f"[moy avant: {avg_before:.1f}] Prep={s['prep']:.0f}ms GPU={s['gpu']:.0f}ms Draws={int(s['draws'])}"
                                    if avg_before
                                    else f"FPS bas: {s['fps']:.1f} fps ({s['ms']:.0f} ms) Prep={s['prep']:.0f}ms GPU={s['gpu']:.0f}ms"
                                )
                                self._save_single_shot("FPS_DROP", reason, frame, hw, payload)
                        else:
                            cd = 8.0 if kind == "LOG_TEX_CHANGED_SILENT" else 2.5
                            if self._can_fire(kind, cd):
                                self._save_single_shot(kind, msg, frame, hw, payload)

                    # 4. Baseline périodique en gameplay actif
                    now = time.monotonic()
                    if is_world and (now - self.last_baseline_ts) >= self.args.baseline_every:
                        self.last_baseline_ts = now
                        self._save_single_shot("BASELINE", "Reference gameplay periodique", frame, hw)

                    # 5. Hotkeys F8 (snap) et F7 (rafale)
                    f8_now = bool(_user32.GetAsyncKeyState(VK_F8) & 0x8000)
                    if f8_now and not self._f8_down:
                        self._save_single_shot("MANUAL_SNAP", "Capture manuelle F8", frame, hw)
                    self._f8_down = f8_now

                    f7_now = bool(_user32.GetAsyncKeyState(VK_F7) & 0x8000)
                    if f7_now and not self._f7_down:
                        self._capture_manual_burst(sct, rect, hw)
                    self._f7_down = f7_now

                # 6. Timeline CSV (1 Hz)
                now = time.monotonic()
                if now - self.last_timeline_ts >= 1.0:
                    self.last_timeline_ts = now
                    lf = self.tailer.state.last_fps or {}
                    with self.timeline_path.open("a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerow([
                            datetime.now().strftime("%H:%M:%S"),
                            lf.get("fps", ""), lf.get("ms", ""), lf.get("prep", ""),
                            lf.get("gpu", ""), lf.get("back_ms", ""),
                            int(lf["draws"]) if "draws" in lf else "",
                            int(lf["dropped"]) if "dropped" in lf else "",
                            int(lf["up_n"]) if "up_n" in lf else "",
                            int(lf["waiting"]) if "waiting" in lf else "",
                            int(lf["unmapped"]) if "unmapped" in lf else "",
                            hw.get("gpu_pct", ""), hw.get("vram_used_mb", ""),
                            hw.get("vram_total_mb", ""), hw.get("gpu_temp_c", ""),
                            hw.get("cpu_pct", ""), hw.get("ram_free_mb", ""),
                            hw.get("game_ram_mb", ""),
                            round(frame.luma_mean, 1) if frame else "",
                            round(flash_mad, 2),
                        ])

                # Cadence ~6 Hz (160 ms)
                elapsed = time.monotonic() - t0
                sleep_s = max(0.02, self.args.interval - elapsed)
                time.sleep(sleep_s)
        finally:
            if dx_cam is not None:
                try:
                    dx_cam.release()
                except Exception:
                    pass

        rep = self.write_report()
        print(f"\nRapport pro genere : {rep}", flush=True)
        return 0


def run_audit_only(log_path: Path) -> int:
    tailer = PlayLogTailer(log_path)
    if not log_path.is_file():
        print(f"Log introuvable : {log_path}")
        return 1
    alerts = tailer.poll()
    st = tailer.state
    print("=== VIce Pro Audit (statique) ===")
    for h in st.header[:5]:
        print(" ", h)
    print(f"Errors: {st.errors} | Warnings: {st.warnings} | World samples: {len(st.world_samples)}")
    if st.world_samples:
        fps_vals = sorted(x["fps"] for x in st.world_samples)
        p10 = fps_vals[max(0, int(len(fps_vals) * 0.10))]
        tail = st.world_samples[-12:]
        avg = lambda k: statistics.mean(x[k] for x in tail)
        last = tail[-1]
        print(
            f"Steady (12): FPS={avg('fps'):.1f} (min={fps_vals[0]:.1f}, p10={p10:.1f}, max={fps_vals[-1]:.1f}) | "
            f"Frame={avg('ms'):.1f} ms | Prep={avg('prep'):.1f} ms (heap {avg('heap_ms'):.1f}, copy {avg('copy_ms'):.1f}) | "
            f"GPU={avg('gpu'):.1f} ms | Back={avg('back_ms'):.1f} ms | Draws={avg('draws'):.0f} (drop {avg('dropped'):.0f})"
        )
        print(
            f"Textures: {int(last['up_n'])} up ({int(last['up_mb'])} MB) | "
            f"waiting={int(last['waiting'])} | unmapped={int(last['unmapped'])}"
        )
    if st.last_queue:
        print(f"Queue: {st.last_queue[0]} slots, {st.last_queue[1]} due (oldest: {st.last_queue[2]} frames)")
    if st.last_sweep:
        print(f"Sweep: {st.last_sweep[0]} looks, avg={st.last_sweep[1]} ms, max={st.last_sweep[2]} ms")
    if st.last_res:
        print(f"MakeResident: {st.last_res[0]} calls, total={st.last_res[1]} ms, max={st.last_res[2]} ms")
    if st.last_map:
        print(f"Mapped waits: {st.last_map[0]} calls, total={st.last_map[1]} ms, max={st.last_map[2]} ms")
    crit = [a for a in alerts if a[0] != "FPS_SAMPLE"]
    if crit:
        print(f"Alertes critiques detectees ({len(crit)}) :")
        seen = set()
        for kind, msg, _ in crit:
            if kind not in seen:
                seen.add(kind)
                print(f"  - [{kind}] {msg[:140]}")
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    print("=== VIce Pro Audit Self-Test ===")
    nvml = NvmlReader()
    gpu = nvml.read()
    print(f"NVML     : {gpu}")
    with mss.mss() as sct:
        mon = sct.monitors[1]
        raw = sct.grab(mon)
        bgr = resize_max(np.asarray(raw)[:, :, :3], 1280)
        print(f"MSS      : OK ({mon['width']}x{mon['height']} -> {bgr.shape[1]}x{bgr.shape[0]})")
    aud = ProAuditor(args)
    aud.tailer.poll()
    hw = aud._collect_hw(None)
    # Vérifie le rendu du bandeau HUD + du détecteur de flash sur deux images synthétiques
    s1 = FrameSnap(0.0, "00:00:00.000", bgr, cv2.cvtColor(cv2.resize(bgr, (320, 180)), cv2.COLOR_BGR2GRAY).astype(np.float32), 100.0, 0.0)
    dark = (bgr * 0.2).astype(np.uint8)
    s2 = FrameSnap(0.16, "00:00:00.160", dark, cv2.cvtColor(cv2.resize(dark, (320, 180)), cv2.COLOR_BGR2GRAY).astype(np.float32), 20.0, 0.5)
    aud.frame_ring.append(s1)
    aud.frame_ring.append(s2)
    is_flash, reason, mad, fb, ff = aud._detect_flash(s1)
    print(f"FlashDet : triggered={is_flash} ({reason})")
    strip = build_flash_strip(s1, s2, s1, "SELF-TEST FLASH", aud._banner_lines(hw, reason))
    test_png = aud.shots_dir / "000_selftest_strip.png"
    cv2.imwrite(str(test_png), strip, [cv2.IMWRITE_PNG_COMPRESSION, 5])
    rep = aud.write_report()
    print(f"Report   : {rep} (OK, png={test_png.stat().st_size} B)")
    return 0 if (is_flash and test_png.is_file() and rep.is_file()) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="VIce Pro Gameplay Auditor")
    p.add_argument("--log", default=str(PLAY_LOG), help="Chemin du play.log")
    p.add_argument("--audit-only", action="store_true", help="Audit statique du play.log sans capture")
    p.add_argument("--self-test", action="store_true", help="Auto-test NVML/mss/détecteur de flash")
    p.add_argument("--until-exit", action="store_true", help="S'arrête automatiquement quand le jeu ferme")
    p.add_argument("--interval", type=float, default=0.16, help="Intervalle capture en secondes (défaut: 0.16s ~6 Hz)")
    p.add_argument("--fps-threshold", type=float, default=9.0, help="Seuil chute FPS absolue (défaut: 9.0)")
    p.add_argument("--fps-cooldown", type=float, default=4.0, help="Cooldown entre captures FPS_DROP (s)")
    p.add_argument("--flash-mad", type=float, default=14.0, help="Sensibilité détection flash A->B->A (MAD)")
    p.add_argument("--flash-cooldown", type=float, default=2.5, help="Cooldown entre captures FLASH (s)")
    p.add_argument("--baseline-every", type=float, default=20.0, help="Capture de référence gameplay toutes les N s")
    p.add_argument("--detect-in-menus", action="store_true", help="Activer la détection de flash aussi hors gameplay")
    args = p.parse_args()

    if args.audit_only:
        return run_audit_only(Path(args.log))
    if args.self_test:
        return run_self_test(args)

    auditor = ProAuditor(args)

    def _on_sig(signum, frame):
        auditor.stop_requested = True

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)
    return auditor.run_live()


if __name__ == "__main__":
    sys.exit(main())
