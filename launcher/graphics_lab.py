"""Visual regression lab. Runs only original synthetic tests, never a title."""
import os
import sys
import subprocess
import struct
import zlib
import html
import re
import hashlib
import argparse
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VIce" / "graphics-lab"
CASES = [
    ("Compute / LDS", "victory-shader", "vice_lds", False, []),
    ("Translated mesh and pixel shaders", "victory-shader", "vice_translated_mesh_draws_triangle", False,
     ["01-solid-triangle", "02-invalid-indices", "03-culled", "04-interpolated-surface", "05-extra-primitive-slots"]),
    ("Multi-group mesh / wave64 logic", "victory-shader", "vice_complex_mesh_grid", False, ["07-grid-1", "07-grid-4", "07-grid-8"]),
    ("GPU-buffer mesh", "victory-shader", "vice_buffer_mesh", False, ["08-buffer-mesh-1", "08-buffer-mesh-4", "08-buffer-mesh-8"]),
    ("Compressed indexed vertex mesh", "victory-shader", "vice_packed_vertex_mesh", False, ["09-packed-vertex-mesh-1", "09-packed-vertex-mesh-4", "09-packed-vertex-mesh-8", "10-packed-stepped-mesh"]),
    ("Wave64 synthetic tests", "victory-shader", "wave64_tests", False, []),
    ("Structured integer vertices", "victory-shader", "vice_integer_vertex_mesh", False,
     [f"11-integer-vertex-mesh-{n}" for n in (1,4,8)]),
    ("Raw integer vertices", "victory-shader", "vice_raw_integer_vertex_mesh", False,
     [f"12-raw-integer-vertex-mesh-{n}" for n in (1,4,8)]),
    ("Partial 16-bit loads", "victory-shader", "vice_d16_vertex_mesh", False,
     [f"13-d16-vertex-mesh-{n}" for n in (1,4,8)]),
    ("Native Direct3D 12 reference", "victory-gpu", "synthetic_mesh_pipeline_draws_color", True, ["06-native-reference"]),
]
LABELS = {
    "01-solid-triangle": "Translated solid geometry",
    "02-invalid-indices": "Invalid indices (expected black)",
    "03-culled": "Culled primitive (expected black)",
    "04-interpolated-surface": "Interpolated mesh / pixel output",
    "05-extra-primitive-slots": "Extra primitive slots",
    "06-native-reference": "Native GPU reference",
    "08-buffer-mesh-1": "Buffer mesh: 32 triangles",
    "08-buffer-mesh-4": "Buffer mesh: 128 triangles",
    "08-buffer-mesh-8": "Buffer mesh: 256 triangles",
    "09-packed-vertex-mesh-1": "Compressed mesh: 32 triangles",
    "09-packed-vertex-mesh-4": "Compressed mesh: 128 triangles",
    "09-packed-vertex-mesh-8": "Compressed mesh: 256 triangles",
    "10-packed-stepped-mesh": "Changed vertex buffer: stepped silhouette",
    "07-grid-1": "32 triangles / 1 group",
    "07-grid-4": "128 triangles / 4 groups",
    "07-grid-8": "256 triangles / 8 groups",
}

for prefix, title in (("11-integer-vertex-mesh", "Structured integer vertices"),
                      ("12-raw-integer-vertex-mesh", "Raw integer vertices"),
                      ("13-d16-vertex-mesh", "Partial 16-bit loads")):
    for n in (1,4,8):
        LABELS[f"{prefix}-{n}"] = f"{title}: {n} groups"


def test_status(output, returncode, missing):
    matches = re.findall(r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored", output)
    if returncode or any(state != "ok" or int(failed) for state, passed, failed, ignored in matches):
        return "FAIL"
    if not matches or not sum(int(passed) for _, passed, _, _ in matches):
        return "NOT VALIDATED"
    if missing or "SKIP:" in output or re.search(r"\bskipped\b", output, re.I):
        return "NOT VALIDATED"
    return "PASS"


def read_ppm(path):
    magic, dims, maxval, rgb = path.read_bytes().split(b"\n", 3)
    w, h = map(int, dims.split())
    if magic != b"P6" or maxval != b"255" or w <= 0 or h <= 0 or len(rgb) != w*h*3:
        raise ValueError("Invalid GPU capture")
    return w,h,rgb


def compare_capture(current, reference):
    w,h,rgb = current
    rw,rh,old = reference
    if (w,h) != (rw,rh):
        return {"status":"DIMENSIONS CHANGED", "changed_pixels":None}
    changed = sum(rgb[i:i+3] != old[i:i+3] for i in range(0,len(rgb),3))
    return {"status":"MATCH" if not changed else "PIXELS CHANGED", "changed_pixels":changed}


def png_from_ppm(path):
    w,h,rgb = read_ppm(path)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))
    raw = b"".join(b"\0"+rgb[y*w*3:(y+1)*w*3] for y in range(h))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w,h,8,2,0,0,0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    path.with_suffix(".png").write_bytes(png)
    return w,h,sum(any(rgb[i:i+3]) for i in range(0,len(rgb),3)),hashlib.sha256(rgb).hexdigest()

def main():
    parser = argparse.ArgumentParser(description="Synthetic graphics regression lab")
    parser.add_argument("--baseline", type=Path, help="Compare to a previously validated capture directory on the same GPU")
    args_cli = parser.parse_args()
    baseline = args_cli.baseline
    if baseline is not None and not baseline.is_dir():
        parser.error("Baseline directory does not exist")
    out = DATA / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True)
    env = dict(os.environ, VICE_LAB_OUTPUT=str(out), NO_COLOR="1")
    results = []
    for title, crate, test, diag, captures in CASES:
        args = ["cargo","test","--locked","-p",crate]
        if diag:
            args += ["--features","diagnostics"]
        args += ["--lib",test,"--","--nocapture","--test-threads=1"]
        print("\n=== "+title+" ===", flush=True)
        proc = subprocess.run(args,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,encoding="utf-8",errors="replace")
        print(proc.stdout,flush=True)
        (out/(test+".log")).write_text(proc.stdout,encoding="utf-8")
        missing = [name for name in captures if not (out/(name+".ppm")).exists()]
        status = test_status(proc.stdout, proc.returncode, missing)
        results.append((title,status,test))
    cards = []
    comparisons = {}
    captures_info = {}
    regression = False
    for name,label in LABELS.items():
        file=out/(name+".ppm")
        if file.exists():
            w,h,lit,digest=png_from_ppm(file)
            captures_info[name] = {"width":w,"height":h,"lit_pixels":lit,"sha256":digest}
            if baseline is not None:
                ref = baseline/(name+".ppm")
                check = compare_capture(read_ppm(file), read_ppm(ref)) if ref.is_file() else {"status":"NO BASELINE","changed_pixels":None}
                comparisons[name] = check
                regression |= check["status"] != "MATCH"
            cards.append(f'<figure><img src="{name}.png"><figcaption>{html.escape(label)}<br>{w} x {h} actual GPU readback<br>{lit}/{w*h} non-black pixels<br>SHA256 {digest[:16]}</figcaption></figure>')
    if baseline is not None:
        for name in LABELS:
            if name not in comparisons:
                comparisons[name] = {"status":"CAPTURE MISSING","changed_pixels":None}
                regression = True
    comparison_html = ""
    if baseline is not None:
        rows = "".join(f"<tr><td>{html.escape(name)}</td><td>{v['status']}</td><td>{v['changed_pixels'] if v['changed_pixels'] is not None else '-'}</td></tr>" for name,v in comparisons.items())
        comparison_html = "<h2>Pixel regression against selected baseline</h2><p>Exact RGB comparison, same GPU and settings required.</p><table>"+rows+"</table>"
        print("BASELINE: "+str(baseline),flush=True)
        print("MATCHED: "+str(sum(v["status"]=="MATCH" for v in comparisons.values()))+"/"+str(len(comparisons)),flush=True)
    (out/"results.json").write_text(json.dumps({"baseline":str(baseline) if baseline else None,"cases":[{"title":title,"status":status,"test":test} for title,status,test in results],"captures":captures_info,"comparisons":comparisons},indent=2),encoding="utf-8")
    table = "".join(f'<tr><td>{html.escape(title)}</td><td>{status}</td><td><a href="{test}.log">Log</a></td></tr>' for title,status,test in results)
    page = """<!doctype html><html><meta charset="utf-8"><title>VIce Graphics Lab</title>
<style>body{background:#101319;color:#eee;font:16px Segoe UI,sans-serif;max-width:1050px;margin:40px auto;padding:0 20px}h1{color:#86ebd3}table{border-collapse:collapse;width:100%}td{padding:12px;border-bottom:1px solid #38404a}a{color:#86ebd3}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}figure{margin:0;padding:15px;background:#1c2430;border-radius:8px}img{width:100%;aspect-ratio:1;image-rendering:pixelated;object-fit:contain;background:black}figcaption{padding-top:10px;line-height:1.6}</style>
<h1>VIce Graphics Lab</h1><p>Original synthetic scenes only. Images are GPU readbacks, not screenshots of a game. The interpolated surface uses a display remapping from [-1,1] to [0,1].</p>"""
    page += "<table>"+table+"</table>"+comparison_html+"<h2>Captured outputs</h2><div class='grid'>"+"".join(cards)+"</div><p>Black invalid/cull captures are intentional. Textures, depth, lighting and complete title compatibility are not validated by this lab.</p></html>"
    (out/"index.html").write_text(page,encoding="utf-8")
    DATA.mkdir(exist_ok=True)
    (DATA/"latest.txt").write_text(str(out/"index.html"),encoding="utf-8")
    print("REPORT: "+str(out/"index.html"),flush=True)
    print("RESULTS: "+", ".join(status for _,status,_ in results),flush=True)
    return int(regression or any(status!="PASS" for _,status,_ in results))

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert test_status("test result: ok. 0 passed; 0 failed; 0 ignored", 0, []) == "NOT VALIDATED"
        assert test_status("test result: ok. 1 passed; 0 failed; 0 ignored", 0, []) == "PASS"
        assert test_status("test result: ok. 1 passed; 0 failed; 0 ignored", 0, ["missing"]) == "NOT VALIDATED"
        assert test_status("test result: FAILED. 0 passed; 1 failed; 0 ignored", 1, []) == "FAIL"
        assert test_status("SKIP: no adapter\n test result: ok. 1 passed; 0 failed; 0 ignored", 0, []) == "NOT VALIDATED"
        assert compare_capture((1,1,b"\x00\x00\x00"),(1,1,b"\x00\x00\x00")) == {"status":"MATCH","changed_pixels":0}
        assert compare_capture((1,1,b"\x00\x01\x00"),(1,1,b"\x00\x00\x00")) == {"status":"PIXELS CHANGED","changed_pixels":1}
        assert compare_capture((1,1,b"\x00"*3),(2,1,b"\x00"*6))["status"] == "DIMENSIONS CHANGED"
        print("Graphics lab status and regression tests passed")
    else:
        sys.exit(main())