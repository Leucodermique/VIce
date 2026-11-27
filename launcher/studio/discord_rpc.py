"""Native Windows Discord Rich Presence (IPC Named Pipe) client for VIce Studio.

Zero external dependencies: speaks Discord's local IPC protocol directly over
`\\\\.\\pipe\\discord-ipc-0..9` using only the Python standard library.
Never stores or requires any bot token — only the public Application Client ID.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import uuid

DISCORD_CLIENT_ID = os.environ.get("VICE_DISCORD_CLIENT_ID", "1575957926378737744")
GITHUB_URL = "https://github.com/Leucodermique/VIce"
DISCORD_INVITE_URL = "https://discord.gg/5Sp2RcDBwY"
VICE_ICON_URL = "https://raw.githubusercontent.com/Leucodermique/VIce/main/.github/assets/discord/icon.png"
GTA6_LOGO_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d6/"
    "Grand_Theft_Auto_VI_logo_%28with_gradient%29.svg/"
    "512px-Grand_Theft_Auto_VI_logo_%28with_gradient%29.svg.png"
)

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


class DiscordIPC:
    """Minimal blocking client for Discord's Windows Named Pipe IPC."""

    def __init__(self, client_id: str = DISCORD_CLIENT_ID):
        self.client_id = str(client_id)
        self._pipe = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._pipe is not None

    def connect(self) -> bool:
        with self._lock:
            if self._pipe is not None:
                return True
            for i in range(10):
                pipe_path = rf"\\.\pipe\discord-ipc-{i}"
                try:
                    pipe = open(pipe_path, "r+b", buffering=0)
                except OSError:
                    continue
                try:
                    self._send_raw(pipe, OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                    op, resp = self._recv_raw(pipe)
                    if op == OP_FRAME and isinstance(resp, dict) and resp.get("evt") == "READY":
                        self._pipe = pipe
                        return True
                except OSError:
                    pass
                try:
                    pipe.close()
                except OSError:
                    pass
            return False

    def close(self) -> None:
        with self._lock:
            if self._pipe is None:
                return
            try:
                self._send_raw(self._pipe, OP_CLOSE, {})
            except OSError:
                pass
            try:
                self._pipe.close()
            except OSError:
                pass
            self._pipe = None

    def set_activity(
        self,
        *,
        details: str,
        state: str,
        start_timestamp: int | None = None,
        large_image: str = VICE_ICON_URL,
        large_text: str = "VIce Studio",
        small_image: str | None = None,
        small_text: str = "VIce PC Engine",
        buttons: list[dict[str, str]] | None = None,
    ) -> dict | None:
        assets: dict[str, str] = {
            "large_image": large_image,
            "large_text": large_text[:128],
        }
        if small_image:
            assets["small_image"] = small_image
            assets["small_text"] = small_text[:128]
        activity: dict = {
            "details": details[:128],
            "state": state[:128],
            "assets": assets,
            "buttons": (
                buttons
                if buttons is not None
                else [
                    {"label": "GitHub VIce", "url": GITHUB_URL},
                    {"label": "Rejoindre Discord", "url": DISCORD_INVITE_URL},
                ]
            )[:2],
        }
        if start_timestamp is not None:
            activity["timestamps"] = {"start": int(start_timestamp)}

        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": os.getpid(),
                "activity": activity,
            },
            "nonce": str(uuid.uuid4()),
        }
        return self._request(payload)

    def clear_activity(self) -> None:
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid()},
            "nonce": str(uuid.uuid4()),
        }
        self._request(payload)

    def _request(self, payload: dict) -> dict | None:
        with self._lock:
            if self._pipe is None:
                return None
            try:
                self._send_raw(self._pipe, OP_FRAME, payload)
                op, resp = self._recv_raw(self._pipe)
                if op == OP_PING:
                    self._send_raw(self._pipe, OP_PONG, resp if isinstance(resp, dict) else {})
                    op, resp = self._recv_raw(self._pipe)
                if op == OP_CLOSE:
                    self._drop_locked()
                    return None
                return resp if isinstance(resp, dict) else None
            except OSError:
                self._drop_locked()
                return None

    def _drop_locked(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.close()
            except OSError:
                pass
            self._pipe = None

    @staticmethod
    def _send_raw(pipe, opcode: int, data: dict) -> None:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        pipe.write(struct.pack("<II", opcode, len(raw)) + raw)
        pipe.flush()

    @staticmethod
    def _recv_exact(pipe, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = pipe.read(size - len(chunks))
            if not chunk:
                raise OSError("Discord IPC pipe closed")
            chunks.extend(chunk)
        return bytes(chunks)

    @classmethod
    def _recv_raw(cls, pipe) -> tuple[int, dict]:
        header = cls._recv_exact(pipe, 8)
        opcode, length = struct.unpack("<II", header)
        if length > 1024 * 1024:
            raise OSError(f"Unexpected Discord IPC payload size: {length}")
        body = cls._recv_exact(pipe, length) if length else b"{}"
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            parsed = {}
        return opcode, parsed


class VicePresenceManager:
    """Background daemon keeping Discord Rich Presence synchronized with VIce Studio."""

    def __init__(self, state_provider, client_id: str = DISCORD_CLIENT_ID, interval: float = 15.0):
        self._state_provider = state_provider
        self._ipc = DiscordIPC(client_id=client_id)
        self._interval = max(5.0, float(interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mode: str | None = None
        self._session_start: int = int(time.time())
        self._last_sent: tuple[str, str, int] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vice-discord-rpc", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        """Trigger an immediate presence refresh (e.g. game launched or stopped)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        try:
            self._ipc.clear_activity()
        except Exception:
            pass
        self._ipc.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._wake.wait(self._interval)
            self._wake.clear()

    def _tick(self) -> None:
        if not self._ipc.connected:
            if not self._ipc.connect():
                self._last_sent = None
                return

        info = self._state_provider() or {}
        running = bool(info.get("running"))
        mode = "game" if running else "launcher"
        if mode != self._last_mode:
            self._last_mode = mode
            self._session_start = int(time.time())

        if running:
            res = str(info.get("resolution") or "1080p")
            fps = info.get("fps")
            details = "Grand Theft Auto VI"
            if isinstance(fps, (int, float)) and fps > 0:
                state = f"En jeu • {res} • {fps:.0f} FPS"
            else:
                state = f"En jeu • {res} • Smooth Motion"
            large_image = GTA6_LOGO_URL
            large_text = "Grand Theft Auto VI"
            small_image = VICE_ICON_URL
            small_text = "VIce PC Engine"
        else:
            details = "VIce Studio"
            state = "Dans le launcher"
            large_image = VICE_ICON_URL
            large_text = "VIce Studio"
            small_image = None
            small_text = "VIce Studio"

        key = (details, state, self._session_start)
        if key == self._last_sent:
            return
        resp = self._ipc.set_activity(
            details=details,
            state=state,
            start_timestamp=self._session_start,
            large_image=large_image,
            large_text=large_text,
            small_image=small_image,
            small_text=small_text,
        )
        if isinstance(resp, dict) and resp.get("evt") != "ERROR":
            self._last_sent = key
        else:
            self._last_sent = None
