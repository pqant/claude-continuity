"""Power-plug endpoint (Phase 13): Kasa HS100 relay control + localhost HTTP.

Lets the watchdog auto-heal the DGX through ANY interruption: a small
launchd-resident HTTP service on 127.0.0.1:9999 (``GET /state``,
``POST /on|/off|/cycle``) drives a TP-LINK HS100 smart plug sitting on the
DGX Spark's power path over the plug's own LAN protocol (legacy Kasa IOT).

Kasa legacy wire format (verified against python-kasa master): TCP, no
auth, 4-byte big-endian length prefix followed by a CHAINED xor stream
(IV 171 — each output byte is ``key ^= plaintext_byte``; a naive
repeating-xor does NOT work). One command per connection; the device
answers on the same connection and both sides close.

Stdlib only: the repo runtime has no third-party dependencies, and the
protocol has been stable for ~10 years.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths

#: Kasa xor stream initialization vector (legacy IOT protocol).
KASA_IV = 171
#: The plug's own TCP port (legacy Kasa IOT) — distinct from the local
#: endpoint's port, which happens to share the number.
DEFAULT_PLUG_PORT = 9999
#: Default seconds the relay stays off during a cycle (enough for the
#: DGX to register the loss of power; safe against a double edge).
DEFAULT_CYCLE_OFF_S = 8
#: The localhost endpoint the watchdog/CLI talk to (loopback only: nothing
#: on the LAN may reach a service that can cut the DGX's power).
ENDPOINT_HOST = "127.0.0.1"
ENDPOINT_PORT = 9999


class PowerPlugError(RuntimeError):
    """The plug is unreachable, hung, or answered something malformed."""


def xor_encode(payload: bytes) -> bytes:
    """Kasa chained-xor: fold each plaintext byte into the running key.

    ``key`` starts at :data:`KASA_IV`; for every byte ``b`` the emitted
    byte is ``key ^ b`` and the key becomes that emitted byte.
    """
    key = KASA_IV
    out = bytearray()
    for b in payload:
        key ^= b
        out.append(key)
    return bytes(out)


def xor_decode(blob: bytes) -> bytes:
    """Inverse of :func:`xor_encode`.

    The first plaintext byte is ``IV ^ enc[0]``; every later byte is the
    xor of the two preceding ciphertext bytes (``enc[i-1] ^ enc[i]``),
    because the running key IS the previous ciphertext byte.
    """
    out = bytearray()
    for i, b in enumerate(blob):
        if i == 0:
            out.append(KASA_IV ^ b)
        else:
            out.append(blob[i - 1] ^ b)
    return bytes(out)


def frame(payload: bytes) -> bytes:
    """Kasa wire framing: 4-byte big-endian length prefix + xor stream.

    The encoded stream is the same length as the plaintext, so the prefix
    is ``len(payload)``."""
    encoded = xor_encode(payload)
    return struct.pack(">I", len(encoded)) + encoded


def unframe(blob: bytes) -> bytes:
    """Inverse of :func:`frame`; raises on a truncated stream."""
    if len(blob) < 4:
        raise PowerPlugError(f"short frame: {len(blob)} bytes")
    (n,) = struct.unpack(">I", blob[:4])
    body = blob[4:4 + n]
    if len(body) != n:
        raise PowerPlugError(f"truncated frame: {len(body)}/{n} bytes")
    return xor_decode(body)


class KasaPlug:
    """A single HS100 over the legacy Kasa LAN protocol (no auth).

    One TCP connection per command (the device's behavior); every command
    gets a fresh :meth:`_request` round-trip.
    """

    def __init__(self, host: str, port: int = DEFAULT_PLUG_PORT,
                 timeout: float = 3.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    # -- wire level ------------------------------------------------------

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise PowerPlugError("plug closed the connection")
            buf += chunk
        return buf

    def _request(self, command: dict) -> dict:
        try:
            with socket.create_connection(
                    (self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(frame(json.dumps(command).encode("utf-8")))
                raw = self._recv_exact(sock, 4)
                (n,) = struct.unpack(">I", raw)
                if n > 1_000_000:
                    raise PowerPlugError(f"implausible reply length {n}")
                return json.loads(unframe(raw + self._recv_exact(sock, n)))
        except PowerPlugError:
            raise
        except (OSError, socket.timeout) as e:
            raise PowerPlugError(
                f"plug unreachable at {self.host}:{self.port}: {e}") from e
        except (ValueError, UnicodeDecodeError, struct.error) as e:
            raise PowerPlugError(f"plug sent an invalid reply: {e}") from e

    # -- commands ----------------------------------------------------------

    def get_sysinfo(self) -> dict:
        res = self._request({"system": {"get_sysinfo": {}}})
        info = res.get("system", {}).get("get_sysinfo")
        if not isinstance(info, dict) or "relay_state" not in info:
            raise PowerPlugError(f"unexpected get_sysinfo reply: {res!r}")
        return info

    def relay_state(self) -> int:
        """0 = off, 1 = on."""
        return int(self.get_sysinfo()["relay_state"])

    def set_relay(self, state: int) -> int:
        """Set the relay and VERIFY by reading it back.

        Never trust a fire-and-forget: returns the actual resulting
        relay state, raises :class:`PowerPlugError` on mismatch.
        """
        if state not in (0, 1):
            raise ValueError("state must be 0 or 1")
        self._request({"system": {"set_relay_state": {"state": state}}})
        actual = self.relay_state()
        if actual != state:
            raise PowerPlugError(
                f"relay verification failed: asked {state}, got {actual}")
        return actual


# ------------------------------------------------- actions layer (Batch C)
# Shared surface for the CLI one-shots AND the HTTP endpoint: each returns a
# JSON-ready dict, degrading to {"ok": False, ...} instead of raising when
# the plug is unconfigured (empty power_plug_ip) or unreachable.


def _plug_from_cfg(cfg: dict) -> KasaPlug | None:
    ip = str(cfg.get("power_plug_ip", "") or "").strip()
    if not ip:
        return None
    return KasaPlug(ip, port=int(cfg.get("power_plug_port",
                                         DEFAULT_PLUG_PORT)))


def read_state(cfg: dict) -> dict:
    """Relay state as {"ok": ..., "state": "on"|"off"|"unknown"[, error]}."""
    plug = _plug_from_cfg(cfg)
    if plug is None:
        return {"ok": False, "state": "unknown",
                "error": "power_plug_ip not set in config"}
    try:
        on = plug.relay_state() == 1
    except PowerPlugError as e:
        return {"ok": False, "state": "unknown", "error": str(e)}
    return {"ok": True, "state": "on" if on else "off"}


def set_relay_state(cfg: dict, *, on: bool) -> dict:
    """Set the relay (verified) and report the actual resulting state."""
    plug = _plug_from_cfg(cfg)
    if plug is None:
        return {"ok": False, "state": "unknown",
                "error": "power_plug_ip not set in config"}
    try:
        st = plug.set_relay(1 if on else 0)
    except (PowerPlugError, ValueError) as e:
        return {"ok": False, "state": "unknown", "error": str(e)}
    return {"ok": True, "state": "on" if st == 1 else "off"}


def cycle(cfg: dict, *, sleep=time.sleep) -> dict:
    """Power cycle: relay off → sleep(power_cycle_off_s) → relay on.

    Every step is verified. If the "on" step fails the result reports the
    ACTUAL relay state (the DGX may be left without power — the caller must
    see that, never a blind success).
    """
    plug = _plug_from_cfg(cfg)
    if plug is None:
        return {"ok": False, "state": "unknown",
                "error": "power_plug_ip not set in config"}
    off_s = float(cfg.get("power_cycle_off_s", DEFAULT_CYCLE_OFF_S))
    try:
        plug.set_relay(0)
        sleep(off_s)
        st = plug.set_relay(1)
    except (PowerPlugError, ValueError) as e:
        try:
            actual = "on" if plug.relay_state() == 1 else "off"
        except PowerPlugError:
            actual = "unknown"
        return {"ok": False, "state": actual, "error": str(e)}
    return {"ok": True, "state": "on" if st == 1 else "off"}


# ------------------------------------------------- HTTP endpoint (Batch D)
# Loopback-only (127.0.0.1): nothing on the LAN can reach a service that can
# cut the DGX's power. Contract: 200 + JSON when the action completed,
# 502 + JSON when the plug side failed, 404 unknown path, 405 wrong method.
# ThreadingHTTPServer: a slow /cycle never blocks /state.


def build_server(cfg: dict, host: str = ENDPOINT_HOST,
                 port: int = ENDPOINT_PORT) -> ThreadingHTTPServer:
    # A real Handler CLASS (socketserver calls
    # RequestHandlerClass(request, client_address, server) and drops the
    # return value — a factory function would never be instantiated).
    # Each build_server call creates a fresh class, so concurrent servers
    # with different cfgs cannot cross-contaminate.
    class Handler(BaseHTTPRequestHandler):
        server_version = "cc-continuity-power-plug/1"
        cfg: dict = {}  # filled in per server below

        def log_message(self, *args) -> None:
            pass  # launchd .err must stay clean; JSON carries the state

        def _send_json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _action(self, fn) -> None:
            res = fn()
            self._send_json(200 if res.get("ok") else 502, res)

        def do_GET(self) -> None:
            if self.path == "/state":
                self._action(lambda: read_state(self.cfg))
            elif self.path in ("/on", "/off", "/cycle"):
                self._send_json(405, {"ok": False, "error": "use POST"})
            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/on":
                self._action(lambda: set_relay_state(self.cfg, on=True))
            elif self.path == "/off":
                self._action(lambda: set_relay_state(self.cfg, on=False))
            elif self.path == "/cycle":
                self._action(lambda: cycle(self.cfg))
            elif self.path == "/state":
                self._send_json(405, {"ok": False, "error": "use GET"})
            else:
                self._send_json(404, {"ok": False, "error": "not found"})

    Handler.cfg = cfg
    return ThreadingHTTPServer((host, port), Handler)


def serve(cfg: dict, host: str = ENDPOINT_HOST,
          port: int = ENDPOINT_PORT) -> None:
    """Run the endpoint until interrupted (the launchd resident path)."""
    server = build_server(cfg, host, port)
    print(f"power-plug endpoint listening on "
          f"http://{host}:{server.server_address[1]} "
          f"(plug {cfg.get('power_plug_ip') or '(unset)'}"
          f":{cfg.get('power_plug_port', DEFAULT_PLUG_PORT)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --- plist / launchctl (Batch F — same pattern as watchdogd) ------------------

#: The LaunchAgent label (plist file name minus .plist).
PLIST_LABEL = "com.claude-continuity.power-plug"


def plist_path(home: Path | str) -> Path:
    return (Path(home) / "Library" / "LaunchAgents"
            / f"{PLIST_LABEL}.plist")


def plist_render(binary: str, home: Path | str) -> str:
    """The LaunchAgent plist. The absolute ``cc-continuity`` path is baked
    at install time (launchd's minimal PATH cannot resolve a bare name)."""
    logs = paths.logs_dir()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key>\n  <string>{PLIST_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        f"    <string>{binary}</string>\n"
        "    <string>power-plug</string>\n    <string>serve</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        f"  <key>WorkingDirectory</key>\n  <string>{home}</string>\n"
        f"  <key>StandardOutPath</key>\n"
        f"  <string>{logs}/power-plug.out</string>\n"
        f"  <key>StandardErrorPath</key>\n"
        f"  <string>{logs}/power-plug.err</string>\n"
        "</dict>\n</plist>\n"
    )


def _launchctl(runner, *args: str) -> bool:
    try:
        proc = runner(["launchctl", *args], capture_output=True, timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_plist(home: Path | str, *, runner=subprocess.run,
                  which=None) -> Path:
    """Resolve the installed binary, write the plist,
    ``launchctl bootstrap gui/<uid> <plist>``. Raises PowerPlugError on
    failure. ``which`` defaults to ``shutil.which`` resolved at call time
    (a def-time default would defeat the tests' monkeypatching)."""
    if which is None:
        which = shutil.which
    binary = which("cc-continuity")
    if binary is None:
        raise PowerPlugError("cc-continuity not on PATH (install first)")
    p = plist_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plist_render(binary, Path(home)), encoding="utf-8")
    if not _launchctl(runner, "bootstrap", f"gui/{os.getuid()}", str(p)):
        raise PowerPlugError(
            "launchctl bootstrap failed — see `cc-continuity doctor`")
    return p


def uninstall_plist(home: Path | str, *, runner=subprocess.run) -> None:
    _launchctl(runner, "bootout", f"gui/{os.getuid()}/{PLIST_LABEL}")
    plist_path(home).unlink(missing_ok=True)


def start_plist(home: Path | str, *, runner=subprocess.run) -> bool:
    return _launchctl(runner, "kickstart",
                      f"gui/{os.getuid()}/{PLIST_LABEL}")


def stop_plist(home: Path | str, *, runner=subprocess.run) -> bool:
    return _launchctl(runner, "bootout",
                      f"gui/{os.getuid()}/{PLIST_LABEL}")


def status_plist(home: Path | str, *, runner=subprocess.run) -> str | None:
    """``launchctl print gui/<uid>/<label>`` output (None when the
    agent is not loaded)."""
    try:
        proc = runner(["launchctl", "print",
                       f"gui/{os.getuid()}/{PLIST_LABEL}"],
                      capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if isinstance(out, bytes):  # fakes may answer with str
        out = out.decode(errors="replace")
    return out
