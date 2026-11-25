from pathlib import Path
base=Path(__file__).resolve().parent
p=base/"studio_server.py"
s=p.read_text(encoding="utf-8")
assert 'if req.path=="/" and parse_qs(req.query).get("key")==[TOKEN]:' in s
s=s.replace('from urllib.parse import urlsplit,parse_qs','from urllib.parse import urlsplit')
s=s.replace('''        req=urlsplit(self.path)
        if req.path=="/" and parse_qs(req.query).get("key")==[TOKEN]:
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store");self.end_headers()
            self.wfile.write((BASE/"studio.html").read_bytes());return''','''        req=urlsplit(self.path)
        if not self.local_host():
            self.send_error(403);return
        if req.path=="/":
            html=(BASE/"studio.html").read_text(encoding="utf-8").replace("__STUDIO_SESSION_TOKEN__",TOKEN)
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store");self.send_header("Referrer-Policy","no-referrer");self.send_header("X-Frame-Options","DENY");self.end_headers()
            self.wfile.write(html.encode("utf-8"));return
        if req.path=="/health":
            self.send_response(200);self.send_header("Content-Type","text/plain");self.end_headers();self.wfile.write(b"VIce Studio local v3");return''')
s=s.replace('    def do_POST(self):\n        global DEV,LAST_ATTEMPT','''    def local_host(self):
        return self.headers.get("Host")==f"127.0.0.1:{self.server.server_port}"
    def do_POST(self):
        global DEV,LAST_ATTEMPT
        if not self.local_host():
            self.send_error(403);return''')
s=s.replace('''    server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
    url=f"http://127.0.0.1:{server.server_port}/?key={TOKEN}"
    print(url,flush=True)
    if "--no-browser" not in sys.argv:''','''    existing=False
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
    if "--no-browser" not in sys.argv:''')
s=s.replace('    server.serve_forever()','    if not existing:server.serve_forever()')
p.write_text(s,encoding="utf-8")
p=base/"studio.html";s=p.read_text(encoding="utf-8")
assert 'const key=new URLSearchParams(location.search).get("key");' in s
s=s.replace('const key=new URLSearchParams(location.search).get("key");','const key="__STUDIO_SESSION_TOKEN__";')
p.write_text(s,encoding="utf-8")
print("Stable homepage and single-instance startup patched")
