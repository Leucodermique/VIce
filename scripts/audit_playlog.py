"""VIce PlayLog Auditor — objective FPS, GPU, CPU, freeze & texture telemetry."""
from __future__ import annotations

import os
import re
import statistics
import sys
from pathlib import Path

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
SWEEP_RE = re.compile(r"(\d+)\s+looks for textures to let go,\s+(\d+(?:\.\d+)?)\s+ms each,\s+the longest\s+(\d+(?:\.\d+)?)")
RES_RE = re.compile(r"new memory waited for.*?: (\d+) times,\s+(\d+(?:\.\d+)?)\s+ms,\s+the longest\s+(\d+(?:\.\d+)?)\s+ms")
MAP_RE = re.compile(r"new card memory waited for.*?: (\d+) times,\s+(\d+(?:\.\d+)?)\s+ms,\s+the longest\s+(\d+(?:\.\d+)?)\s+ms")


def audit(path: Path) -> int:
    if not path.is_file():
        print(f"Log introuvable : {path}")
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = [l for l in lines[:8] if l.strip()]
    samples = []
    last_queue = None
    last_sweep = None
    last_res = None
    last_map = None
    errors = 0
    warns = 0

    for line in lines:
        if " ERROR " in line:
            errors += 1
        elif " WARN " in line:
            warns += 1
        m = FPS_RE.search(line)
        if m:
            d = {k: float(v) for k, v in m.groupdict().items()}
            if d["draws"] >= 500:  # world / active gameplay frames
                samples.append(d)
        if m := QUEUE_RE.search(line):
            last_queue = m.groups()
        if m := SWEEP_RE.search(line):
            last_sweep = m.groups()
        if m := RES_RE.search(line):
            last_res = m.groups()
        if m := MAP_RE.search(line):
            last_map = m.groups()

    print("=== VIce PlayLog Audit ===")
    for h in header[:5]:
        print(" ", h)
    print(f"Errors: {errors} | Warnings: {warns} | World FPS samples: {len(samples)}")
    if samples:
        tail = samples[-12:]
        avg = lambda k: statistics.mean(x[k] for x in tail)
        print(
            f"Steady-state (last {len(tail)} world samples): "
            f"FPS={avg('fps'):.1f} (max={max(x['fps'] for x in tail):.1f}) | "
            f"Frame={avg('ms'):.1f} ms | "
            f"Prep={avg('prep'):.1f} ms (copies {avg('copy_ms'):.1f} ms / {avg('copy_mb'):.1f} MB, heap {avg('heap_ms'):.1f} ms) | "
            f"GPU={avg('gpu'):.1f} ms | "
            f"Back={avg('back_ms'):.1f} ms ({avg('back_mb'):.1f} MB) | "
            f"Draws={avg('draws'):.0f}"
        )
        last = tail[-1]
        print(
            f"Textures: {int(last['up_n'])} uploaded ({int(last['up_mb'])} MB) | "
            f"waiting={int(last['waiting'])} | unmapped={int(last['unmapped'])}"
        )
    if last_queue:
        print(f"Queue: {last_queue[0]} slots, {last_queue[1]} due (oldest: {last_queue[2]} frames ago)")
    if last_sweep:
        print(f"Texture sweep: {last_sweep[0]} looks, avg={last_sweep[1]} ms, max={last_sweep[2]} ms")
    if last_res:
        print(f"MakeResident waits: {last_res[0]} calls, total={last_res[1]} ms, max={last_res[2]} ms")
    if last_map:
        print(f"Mapped waits: {last_map[0]} calls, total={last_map[1]} ms, max={last_map[2]} ms")
    return 0


if __name__ == "__main__":
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "VIce" / "launcher" / "play.log"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    sys.exit(audit(target))
