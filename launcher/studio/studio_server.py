from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from collections import Counter
import json, os, re, sys, secrets, threading, subprocess, hashlib, hmac, time
BASE=Path(__file__).resolve().parent
ENGINE=BASE.parents[1]
AUTH_FILE=Path(os.environ["LOCALAPPDATA"])/"VIce"/"studio-auth.json"
LOGDIR=Path(os.environ["LOCALAPPDATA"])/"VIce"/"launcher"
TOKEN=secrets.token_urlsafe(24)
DEV=False
DEV_LOCK=threading.Lock()
LAST_ATTEMPT=0.0
PROC_LOCK=threading.Lock()
PROC=None
ANSI=re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_CATEGORIES=(
    ("host pipeline refused","Pipeline rejected"),
    ("host mesh pipeline refused","Mesh pipeline rejected"),
    ("host root signature refused","Root signature rejected"),
    ("slow host submission","Slow GPU submission"),
    ("no host texture","Texture rejected"),
    ("D3D12:","Direct3D validation message"),
    ("is not supported","Unsupported operation"),
)
def resolve_engine(version):
    release_exe=ENGINE/"target"/"release"/"victory.exe"
    diag_exe=ENGINE/"target"/"diag"/"victory.exe"
    debug_exe=ENGINE/"target"/"debug"/"victory.exe"
    if version=="victory.exe":
        if release_exe.is_file():
            return release_exe
        return diag_exe if diag_exe.is_file() else debug_exe
    if version=="victory-diag.exe":
        return diag_exe
    if version=="victory-debug.exe":
        return debug_exe
    return ENGINE/"launcher"/"versions"/Path(version).name
def resolve_title():
    state_file=LOGDIR/"last_title.txt"
    try:
        if state_file.is_file():
            saved=Path(state_file.read_text(encoding="utf-8").strip())
            if saved.is_file() and saved.suffix.lower()==".exe":
                return saved
    except OSError: pass
    desktop=Path.home()/"Desktop"
    direct=desktop/"GTA6.Xbox.v1.0.2.1-CYBERLEEK"/"game_scarlett_master.exe"
    if direct.is_file(): return direct
    if desktop.is_dir():
        for folder in sorted(desktop.glob("GTA6*")):
            exe=folder/"game_scarlett_master.exe"
            if exe.is_file(): return exe
    return None
def is_running():
    with PROC_LOCK:
        return PROC is not None and PROC.poll() is None
def stop_game():
    with PROC_LOCK:
        p=PROC
    if p is not None and p.poll() is None:
        subprocess.run(["taskkill","/PID",str(p.pid),"/T","/F"],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
def launch_game(cfg):
    global PROC
    engine=resolve_engine(cfg["version"])
    if not engine.is_file():
        raise ValueError("Exécutable moteur introuvable.")
    title=resolve_title()
    if title is None or not title.is_file():
        raise ValueError("Exécutable du jeu (game_scarlett_master.exe) introuvable.")
    cmd=[str(engine),"run",str(title),"--execute","--console",cfg["console"]]
    env=os.environ.copy()
    env["NO_COLOR"]="1"
    # Optimisations max RTX 4070 8 Go / 16 Go RAM (c96b4f2 + #191/#206 + COPY queue 5e758c7 + AVX2 #384 + dstorage notify lot9).
    env["VICTORY_RELAY"]="2"
    env["VICTORY_WIDE_BACK"]="1"
    env["VICTORY_LATE_WAIT_LISTS"]="512"
    env["VICTORY_CARD_BUFFERS_MIN_MB"]="1"
    env["VICTORY_CARD_BUFFERS_MB"]="768"
    env["VICTORY_DSTORAGE_NOTIFY"]="1"
    for k,flag in (("VICTORY_D3D12_WARP",cfg["warp"]),("VICTORY_D3D12_DEBUG",cfg["debug"]),("VICE_SURFACE_STATS",cfg["surface"])):
        env.pop(k,None)
        if flag: env[k]="1"
    with PROC_LOCK:
        if PROC is not None and PROC.poll() is None:
            raise ValueError("Le jeu est déjà en cours d’exécution.")
        LOGDIR.mkdir(parents=True,exist_ok=True)
        current=LOGDIR/"play.log"
        if current.exists():
            try: current.replace(LOGDIR/"play.previous.log")
            except OSError: pass
        proc=subprocess.Popen(cmd,cwd=str(ENGINE),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",creationflags=subprocess.CREATE_NO_WINDOW)
        PROC=proc
    def reader():
        global PROC
        counts=Counter()
        code=1
        try:
            with (LOGDIR/"play.log").open("w",encoding="utf-8") as log:
                log.write("VIce session "+datetime.now().isoformat()+"\n")
                log.write("Engine: "+str(engine)+"\n")
                log.write("Engine build: "+datetime.fromtimestamp(engine.stat().st_mtime).isoformat()+"\n")
                log.write("Direct3D debug requested: "+env.get("VICTORY_D3D12_DEBUG","0")+"\n")
                log.write("Surface stats requested: "+env.get("VICE_SURFACE_STATS","0")+"\n")
                log.write("WARP requested: "+env.get("VICTORY_D3D12_WARP","0")+"\n")
                log.flush()
                for raw in proc.stdout:
                    line=ANSI.sub("",raw)
                    for text,label in LOG_CATEGORIES:
                        if text in line:
                            counts[label]+=1
                            break
                    log.write(line)
                    log.flush()
                code=proc.wait()
                summary="\nDIAGNOSTIC SUMMARY\n"+"\n".join(f"{label}: {n}" for label,n in counts.most_common())+f"\nExit code: {code}\n"
                log.write(summary)
        except OSError: pass
        finally:
            with PROC_LOCK:
                if PROC is proc: PROC=None
    threading.Thread(target=reader,daemon=True).start()
def verify_password(password):
    if not isinstance(password,str) or len(password)>256:return False
    try:
        cfg=json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        calculated=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(cfg["salt"]),600000)
        return hmac.compare_digest(calculated,bytes.fromhex(cfg["hash"]))
    except (OSError,ValueError,KeyError,TypeError):return False
def public_data():
    d=data()
    with DEV_LOCK: enabled=DEV
    d["developer"]=enabled
    d["running"]=is_running()
    if not enabled:
        for k in ("current","previous"):
            session=d[k]
            session.update(counts={},codes={},lines=[],maxFrame=None)
        d["versions"]=[v for v in d["versions"] if v["name"]=="victory.exe"]
        d["source"]=""
    return d
def metadata(p, alias=None):
    try:
        s=p.stat()
        return dict(name=alias or p.name,date=datetime.fromtimestamp(s.st_mtime).isoformat(),size=s.st_size)
    except OSError: return None
def session(name):
    p=LOGDIR/name
    try:
        raw=p.read_text(encoding="utf-8",errors="replace")
        lines=re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]","",raw).splitlines()
    except OSError: lines=[]
    counts=Counter()
    codes=Counter()
    frames=[]
    exit_code=None
    start=None
    for l in lines:
        if "D3D12:" in l:
            counts["validation"]+=1
            m=re.search(r"id=(\d+)",l)
            if m:codes[m[1]]+=1
        if "is not supported" in l:counts["unsupported"]+=1
        if "host pipeline refused" in l or "host mesh pipeline refused" in l:counts["rejected"]+=1
        if "slow host submission" in l:counts["slow"]+=1
        if "host GPU failure" in l:counts["fatal"]+=1
        if l.startswith("Exit code:"):exit_code=l.split(":",1)[1].strip()
        if l.startswith("VIce session "):start=l.removeprefix("VIce session ")
        m=re.search(r"\bframe=(\d+)",l)
        if m:frames.append(int(m[1]))
    return dict(meta=metadata(p),counts=counts,codes=codes,exit=exit_code,start=start,maxFrame=max(frames,default=None),lines=lines)
def data():
    cur=session("play.log")
    prev=session("play.previous.log");prev.pop("lines")
    release_exe=ENGINE/"target"/"release"/"victory.exe"
    diag_exe=ENGINE/"target"/"diag"/"victory.exe"
    debug_exe=ENGINE/"target"/"debug"/"victory.exe"
    primary=release_exe if release_exe.is_file() else (diag_exe if diag_exe.is_file() else debug_exe)
    files=[metadata(primary,alias="victory.exe")]
    if release_exe.is_file() and diag_exe.is_file():
        files.append(metadata(diag_exe,alias="victory-diag.exe"))
    if debug_exe.is_file() and primary!=debug_exe:
        files.append(metadata(debug_exe,alias="victory-debug.exe"))
    files+=[metadata(p) for p in sorted((ENGINE/"launcher"/"versions").glob("*.exe"),reverse=True)]
    return dict(current=cur,previous=prev,versions=[x for x in files if x],source=str(ENGINE))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlsplit
        req=urlsplit(self.path)
        if not self.local_host():
            self.send_error(403);return
        if req.path=="/":
            html=(BASE/"studio.html").read_text(encoding="utf-8").replace("__STUDIO_SESSION_TOKEN__",TOKEN)
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store");self.send_header("Referrer-Policy","no-referrer");self.send_header("X-Frame-Options","DENY");self.end_headers()
            self.wfile.write(html.encode("utf-8"));return
        if req.path=="/health":
            self.send_response(200);self.send_header("Content-Type","text/plain");self.end_headers();self.wfile.write(b"VIce Studio local v3");return
        if req.path in ("/art/sunset.svg","/art/vice-logo.svg"):
            name=req.path.rsplit("/",1)[1]
            asset=BASE/name
            if asset.is_file():
                self.send_response(200);self.send_header("Content-Type","image/svg+xml");self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(asset.read_bytes());return
        if req.path=="/api/state" and self.headers.get("X-Studio-Key")==TOKEN:
            b=json.dumps(public_data(),ensure_ascii=False).encode()
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(b);return
        self.send_error(404)
    def local_host(self):
        return self.headers.get("Host")==f"127.0.0.1:{self.server.server_port}"
    def do_POST(self):
        global DEV,LAST_ATTEMPT
        if not self.local_host():
            self.send_error(403);return
        if self.path not in ("/api/open-launcher","/api/stop","/api/developer") or self.headers.get("X-Studio-Key")!=TOKEN:
            self.send_error(404);return
        if self.path=="/api/stop":
            stop_game()
            self.send_response(204);self.end_headers();return
        try:
            n=int(self.headers.get("Content-Length","0"))
            if not 0<n<=4096:raise ValueError("Invalid request")
            cfg=json.loads(self.rfile.read(n))
            if not isinstance(cfg,dict):raise ValueError("Invalid configuration")
            if self.path=="/api/developer":
                with DEV_LOCK:
                    if cfg.get("enabled") is False:
                        DEV=False
                    elif cfg.get("enabled") is True:
                        now=time.monotonic()
                        if now-LAST_ATTEMPT<1.0:
                            self.send_error(429);return
                        LAST_ATTEMPT=now
                        if not verify_password(cfg.get("password")):
                            self.send_error(403);return
                        DEV=True
                    else:raise ValueError("Invalid mode")
                self.send_response(204);self.end_headers();return
            with DEV_LOCK: developer=DEV
            versions={x["name"] for x in data()["versions"]}
            if cfg.get("version") not in versions or cfg.get("console") not in {"series-x","series-s"}:raise ValueError("Invalid profile")
            if not all(type(cfg.get(k)) is bool for k in ("debug","surface","warp")):raise ValueError("Invalid options")
            if not developer and (cfg["version"]!="victory.exe" or any(cfg[k] for k in ("debug","surface","warp"))):
                self.send_error(403);return
            cfg={k:cfg[k] for k in ("version","console","debug","surface","warp")}
            launch_game(cfg)
            self.send_response(204);self.end_headers()
        except (ValueError,TypeError,OSError,KeyError):
            self.send_error(400)
    def log_message(self,*args):pass
if __name__=="__main__":
    if "--self-test" in sys.argv:
        d=data();assert "current" in d and isinstance(d["current"]["lines"],list);print("Data test OK",len(d["current"]["lines"]),"lines");sys.exit()
    # Stable local origin preserves UI preferences between normal launches.
    port=0 if "--no-browser" in sys.argv else 17642
    existing=False
    try:
        server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
    except OSError:
        if not port:raise
        from urllib.request import urlopen
        try:
            with urlopen(f"http://127.0.0.1:{port}/health",timeout=2) as r:
                existing=r.read()==b"VIce Studio local v3"
        except OSError:pass
        if not existing:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0,"Le port local de VIce Studio est occupé. Fermez l'autre application puis réessayez.","VIce Studio",0x10)
            sys.exit(1)
    url=f"http://127.0.0.1:{port if existing else server.server_port}/"
    if "--no-browser" in sys.argv:
        print(url+f"?key={TOKEN}",flush=True)
    if "--no-browser" not in sys.argv and "--headless" not in sys.argv:
        chrome=Path(os.environ.get("ProgramFiles",r"C:\Program Files"))/"Google/Chrome/Application/chrome.exe"
        if chrome.is_file():
            subprocess.Popen([str(chrome),"--app="+url,"--window-size=1320,850","--user-data-dir="+str(Path(os.environ["LOCALAPPDATA"])/"VIce"/"studio-browser")])
        else:
            import webbrowser;webbrowser.open(url)
    if not existing:server.serve_forever()
