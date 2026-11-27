"""VIce Agent Bridge — Local collaboration server & CLI for Antigravity <-> ChatGPT Astra.

Zero external dependencies (Python standard library only).
- HTTP API + live web dashboard on http://127.0.0.1:17655
- Persistent JSON state in %LOCALAPPDATA%/VIce/agent_bridge/board.json
- Built-in CLI (`serve`, `send`, `read`, `task`, `done`, `lock`, `unlock`, `status`)

Examples:
    python scripts/agent_bridge.py serve
    python scripts/agent_bridge.py send --from antigravity --to astra --topic "Sync" --body "Salut Astra !"
    python scripts/agent_bridge.py read --since 0
    python scripts/agent_bridge.py task --owner astra --title "Atlas 18 icones PC" --files "scripts/generate_pc_prompts_atlas.py,launcher/studio/studio.html"
"""

from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = int(os.environ.get("VICE_BRIDGE_PORT", "17655"))
BASE_URL = f"http://{HOST}:{PORT}"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VIce" / "agent_bridge"
STATE_FILE = STATE_DIR / "board.json"

LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_state() -> dict:
    return {
        "next_msg_id": 1,
        "next_task_id": 1,
        "messages": [],
        "tasks": [],
        "locks": {},
    }


def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.is_file():
        return _default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "messages" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return _default_state()


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


HTML_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>VIce Agent Bridge — Antigravity ↔ Astra</title>
<style>
  :root { --bg:#0d0b16; --card:#171326; --border:#2d2547; --accent:#a855f7; --cyan:#22d3ee; --pink:#f472b6; --text:#eef0f8; --muted:#9ca3af; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:16px 24px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; background:#120e20; }
  header h1 { margin:0; font-size:18px; background:linear-gradient(90deg,#c084fc,#f472b6,#fb923c); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .grid { display:grid; grid-template-columns: 2fr 1fr; gap:16px; padding:16px 24px; max-width:1400px; margin:0 auto; }
  .panel { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; }
  h2 { margin:0 0 12px 0; font-size:14px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }
  .msg { border:1px solid var(--border); border-radius:10px; padding:12px; margin-bottom:10px; background:#110e1d; }
  .msg-head { display:flex; justify-content:space-between; font-size:12px; margin-bottom:6px; color:var(--muted); }
  .badge { padding:2px 8px; border-radius:999px; font-weight:600; font-size:11px; }
  .from-antigravity { background:#3b1d5e; color:#e9d5ff; }
  .from-astra { background:#0f3a4a; color:#a5f3fc; }
  .from-user { background:#4a1d34; color:#fbcfe8; }
  .msg-body { white-space:pre-wrap; font-size:13.5px; line-height:1.45; }
  .task { border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:8px; background:#110e1d; font-size:13px; }
  .task-done { opacity:0.55; text-decoration:line-through; }
  .lock { font-family:Consolas,monospace; font-size:12px; padding:6px 8px; background:#1f1633; border-radius:6px; margin-bottom:6px; }
  form { display:flex; flex-direction:column; gap:8px; margin-top:12px; border-top:1px solid var(--border); padding-top:12px; }
  input, select, textarea, button { background:#0f0c1a; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:8px 10px; font:inherit; font-size:13px; }
  button { background:linear-gradient(90deg,#9333ea,#ec4899); border:none; font-weight:600; cursor:pointer; }
</style>
</head>
<body>
<header>
  <h1>VIce Agent Bridge — Antigravity ↔ ChatGPT 6.2 Astra</h1>
  <span id="status" style="font-size:12px;color:#34d399;">● Connecté</span>
</header>
<div class="grid">
  <div class="panel">
    <h2>Fil de discussion</h2>
    <div id="messages"></div>
    <form id="sendForm">
      <div style="display:flex;gap:8px;">
        <select id="from"><option value="user">user</option><option value="antigravity">antigravity</option><option value="astra">astra</option></select>
        <select id="to"><option value="all">→ tous</option><option value="astra">→ astra</option><option value="antigravity">→ antigravity</option></select>
        <input id="topic" placeholder="Sujet" style="flex:1;">
      </div>
      <textarea id="body" rows="3" placeholder="Écrire un message aux agents..."></textarea>
      <button type="submit">Envoyer</button>
    </form>
  </div>
  <div style="display:flex;flex-direction:column;gap:16px;">
    <div class="panel">
      <h2>Tâches partagées</h2>
      <div id="tasks"></div>
    </div>
    <div class="panel">
      <h2>Verrous de fichiers actifs</h2>
      <div id="locks"></div>
    </div>
  </div>
</div>
<script>
function esc(s){return String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function refresh(){
  try {
    const r = await fetch('/api/state');
    const st = await r.json();
    document.getElementById('messages').innerHTML = st.messages.map(m =>
      `<div class="msg"><div class="msg-head"><div><span class="badge from-${esc(m.from)}">${esc(m.from)}</span> → <b>${esc(m.to)}</b> · ${esc(m.topic||'')}</div><span>#${m.id} · ${esc(m.ts)}</span></div><div class="msg-body">${esc(m.body)}</div>${m.files && m.files.length ? `<div style="margin-top:6px;font-size:11px;color:#9ca3af;">Fichiers: ${m.files.map(esc).join(', ')}</div>`:''}</div>`
    ).join('') || '<div style="color:#9ca3af;font-size:13px;">Aucun message pour le moment.</div>';
    document.getElementById('tasks').innerHTML = st.tasks.map(t =>
      `<div class="task ${t.status==='done'?'task-done':''}"><b>#${t.id} [${esc(t.status)}]</b> (${esc(t.owner)}) — ${esc(t.title)}${t.notes?`<div style="color:#9ca3af;font-size:12px;margin-top:4px;">${esc(t.notes)}</div>`:''}</div>`
    ).join('') || '<div style="color:#9ca3af;font-size:13px;">Aucune tâche.</div>';
    const locks = Object.entries(st.locks || {});
    document.getElementById('locks').innerHTML = locks.map(([f, info]) =>
      `<div class="lock">🔒 ${esc(f)} <span style="color:#c084fc;">(${esc(info.owner)})</span></div>`
    ).join('') || '<div style="color:#9ca3af;font-size:13px;">Aucun verrou actif.</div>';
  } catch (e) {}
}
document.getElementById('sendForm').onsubmit = async (e) => {
  e.preventDefault();
  const body = document.getElementById('body').value.trim();
  if (!body) return;
  await fetch('/api/messages', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      from: document.getElementById('from').value,
      to: document.getElementById('to').value,
      topic: document.getElementById('topic').value.trim() || 'Message',
      body
    })
  });
  document.getElementById('body').value = '';
  refresh();
};
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class BridgeHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > 2 * 1024 * 1024:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            raw = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "VIce Agent Bridge"})
            return
        if parsed.path == "/api/state":
            with LOCK:
                self._send_json(200, load_state())
            return
        if parsed.path == "/api/messages":
            qs = parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0])
            target = qs.get("for", [""])[0].strip().lower()
            with LOCK:
                msgs = [
                    m
                    for m in load_state()["messages"]
                    if m["id"] > since and (not target or m.get("to") in ("all", target))
                ]
            self._send_json(200, {"messages": msgs})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        with LOCK:
            st = load_state()
            if parsed.path == "/api/messages":
                msg = {
                    "id": st["next_msg_id"],
                    "ts": _now(),
                    "from": str(payload.get("from", "unknown")).strip().lower(),
                    "to": str(payload.get("to", "all")).strip().lower(),
                    "topic": str(payload.get("topic", "Message")).strip(),
                    "body": str(payload.get("body", "")).strip(),
                    "files": [str(f) for f in payload.get("files", []) if f],
                }
                st["next_msg_id"] += 1
                st["messages"].append(msg)
                save_state(st)
                self._send_json(200, {"ok": True, "message": msg})
                return

            if parsed.path == "/api/tasks":
                task = {
                    "id": st["next_task_id"],
                    "ts": _now(),
                    "owner": str(payload.get("owner", "unassigned")).strip().lower(),
                    "title": str(payload.get("title", "")).strip(),
                    "status": str(payload.get("status", "open")).strip().lower(),
                    "files": [str(f) for f in payload.get("files", []) if f],
                    "notes": str(payload.get("notes", "")).strip(),
                }
                st["next_task_id"] += 1
                st["tasks"].append(task)
                save_state(st)
                self._send_json(200, {"ok": True, "task": task})
                return

            if parsed.path == "/api/tasks/update":
                tid = int(payload.get("id", 0))
                for t in st["tasks"]:
                    if t["id"] == tid:
                        if "status" in payload:
                            t["status"] = str(payload["status"]).strip().lower()
                        if "notes" in payload:
                            t["notes"] = str(payload["notes"]).strip()
                        if "owner" in payload:
                            t["owner"] = str(payload["owner"]).strip().lower()
                        save_state(st)
                        self._send_json(200, {"ok": True, "task": t})
                        return
                self._send_json(404, {"error": "Task not found"})
                return

            if parsed.path == "/api/lock":
                owner = str(payload.get("owner", "unknown")).strip().lower()
                files = [str(f).replace("\\", "/") for f in payload.get("files", []) if f]
                conflicts = {
                    f: st["locks"][f]
                    for f in files
                    if f in st["locks"] and st["locks"][f]["owner"] != owner
                }
                if conflicts:
                    self._send_json(409, {"ok": False, "conflicts": conflicts})
                    return
                for f in files:
                    st["locks"][f] = {"owner": owner, "ts": _now()}
                save_state(st)
                self._send_json(200, {"ok": True, "locks": st["locks"]})
                return

            if parsed.path == "/api/unlock":
                owner = str(payload.get("owner", "")).strip().lower()
                files = [str(f).replace("\\", "/") for f in payload.get("files", []) if f]
                if not files and owner:
                    st["locks"] = {k: v for k, v in st["locks"].items() if v["owner"] != owner}
                else:
                    for f in files:
                        if f in st["locks"] and (not owner or st["locks"][f]["owner"] == owner):
                            del st["locks"][f]
                save_state(st)
                self._send_json(200, {"ok": True, "locks": st["locks"]})
                return

        self._send_json(404, {"error": "Not found"})

    def log_message(self, *args) -> None:
        pass


def _api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="VIce Agent Bridge (Antigravity <-> Astra)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run the local collaboration HTTP server")
    sub.add_parser("status", help="Show board summary")

    p_read = sub.add_parser("read", help="Read messages")
    p_read.add_argument("--since", type=int, default=0)
    p_read.add_argument("--for", dest="target", default="")

    p_send = sub.add_parser("send", help="Post a message")
    p_send.add_argument("--from", dest="sender", required=True)
    p_send.add_argument("--to", default="all")
    p_send.add_argument("--topic", default="Sync")
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--files", default="")

    p_task = sub.add_parser("task", help="Create a shared task")
    p_task.add_argument("--owner", required=True)
    p_task.add_argument("--title", required=True)
    p_task.add_argument("--status", default="open")
    p_task.add_argument("--files", default="")
    p_task.add_argument("--notes", default="")

    p_done = sub.add_parser("done", help="Mark a task done/in_progress")
    p_done.add_argument("--id", type=int, required=True)
    p_done.add_argument("--status", default="done")
    p_done.add_argument("--notes", default="")

    p_lock = sub.add_parser("lock", help="Lock files for an agent")
    p_lock.add_argument("--owner", required=True)
    p_lock.add_argument("--files", required=True)

    p_unlock = sub.add_parser("unlock", help="Unlock files for an agent")
    p_unlock.add_argument("--owner", required=True)
    p_unlock.add_argument("--files", default="")

    args = ap.parse_args()

    if args.cmd == "serve":
        srv = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
        print(f"VIce Agent Bridge actif sur {BASE_URL} (board: {STATE_FILE})", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "status":
        st = _api("GET", "/api/state")
        print(f"Messages: {len(st['messages'])} | Taches: {len(st['tasks'])} | Verrous: {len(st['locks'])}")
        for t in st["tasks"]:
            print(f"  [Task #{t['id']} {t['status']}] ({t['owner']}) {t['title']}")
        for f, info in st["locks"].items():
            print(f"  [Lock] {f} -> {info['owner']}")
        return 0

    if args.cmd == "read":
        q = f"/api/messages?since={args.since}" + (f"&for={args.target}" if args.target else "")
        res = _api("GET", q)
        for m in res["messages"]:
            print(f"[#{m['id']} {m['ts']}] {m['from']} -> {m['to']} ({m['topic']}):\n{m['body']}\n")
        return 0

    if args.cmd == "send":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        res = _api("POST", "/api/messages", {
            "from": args.sender,
            "to": args.to,
            "topic": args.topic,
            "body": args.body,
            "files": files,
        })
        print(f"Message #{res['message']['id']} envoye.")
        return 0

    if args.cmd == "task":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        res = _api("POST", "/api/tasks", {
            "owner": args.owner,
            "title": args.title,
            "status": args.status,
            "files": files,
            "notes": args.notes,
        })
        print(f"Tache #{res['task']['id']} creee.")
        return 0

    if args.cmd == "done":
        body = {"id": args.id, "status": args.status}
        if args.notes:
            body["notes"] = args.notes
        _api("POST", "/api/tasks/update", body)
        print(f"Tache #{args.id} -> {args.status}.")
        return 0

    if args.cmd == "lock":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        _api("POST", "/api/lock", {"owner": args.owner, "files": files})
        print("Verrous poses:", ", ".join(files))
        return 0

    if args.cmd == "unlock":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        _api("POST", "/api/unlock", {"owner": args.owner, "files": files})
        print("Verrous liberes.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
