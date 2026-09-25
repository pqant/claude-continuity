"""power_plug.py: Kasa HS100 legacy protocol + localhost HTTP endpoint (Phase 13).

The fake device (``FakeKasa``) speaks the real wire format on
127.0.0.1:ephemeral — one connection per command, like the physical plug.
The absolute wire format is pinned by a hand-computed vector so the fake
can safely reuse the production framing without making the round-trip
tests circular.
"""

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest

from cc_continuity import power_plug


# --------------------------------------------------------- framing (Batch A)
# Kasa legacy IOT protocol: 4-byte big-endian length prefix + a CHAINED
# xor stream (IV 171; each output byte folds the plaintext in:
# key ^= byte). A naive repeating-xor-171 would NOT match the vector.


def test_xor_known_vector():
    # Hand-computed: IV = 171 (0xAB).
    #   byte 0: 0xAB ^ 0x41 ("A") = 0xEA
    #   key is now 0xEA; byte 1: 0xEA ^ 0x42 ("B") = 0xA8
    # A repeating-xor impl would emit 0xE9 for byte 1.
    assert power_plug.xor_encode(b"AB") == b"\xea\xa8"
    assert power_plug.xor_decode(b"\xea\xa8") == b"AB"


def test_xor_single_byte_roundtrip():
    # length-1 payload exercises the i == 0 decrypt branch only
    assert power_plug.xor_decode(power_plug.xor_encode(b"A")) == b"A"


def test_xor_roundtrip_realistic_payload():
    payload = json.dumps(
        {"system": {"get_sysinfo": {"relay_state": 1}}}).encode("utf-8")
    assert power_plug.xor_decode(power_plug.xor_encode(payload)) == payload


# ---------------------------------------------------------------- fake device


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


class FakeKasa:
    """A stand-in HS100: legacy Kasa protocol on 127.0.0.1:ephemeral,
    one connection per command (like the physical plug)."""

    def __init__(self, initial: int = 1):
        self.relay = initial
        self.transitions: list[int] = []   # relay values in set order
        self.commands: list[dict] = []     # decoded request payloads
        self._conn = 0
        # 1-based index of the connection to drop (relay-dying simulation);
        # None = never.
        self.drop_on_connection: int | None = None
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(5)
                self._conn += 1
                if (self.drop_on_connection is not None
                        and self._conn == self.drop_on_connection):
                    self.drop_on_connection = None
                    return  # die silently, like a relay mid-cycle
                hdr = _recv_exact(conn, 4)
                (n,) = struct.unpack(">I", hdr)
                cmd = json.loads(power_plug.xor_decode(_recv_exact(conn, n)))
                self.commands.append(cmd)
                syscmd = cmd.get("system", {})
                if "get_sysinfo" in syscmd:
                    resp = {"system": {"get_sysinfo": {"relay_state": self.relay}}}
                elif "set_relay_state" in syscmd:
                    self.relay = int(syscmd["set_relay_state"]["state"])
                    self.transitions.append(self.relay)
                    resp = {"system": {"set_relay_state": {}}}
                else:
                    resp = {"error_code": -1}
                body = power_plug.xor_encode(json.dumps(resp).encode("utf-8"))
                conn.sendall(struct.pack(">I", len(body)) + body)
        except (OSError, struct.error, ValueError):
            pass  # client went away

    def cfg(self, **over) -> dict:
        base = {"power_plug_ip": "127.0.0.1", "power_plug_port": self.port,
                "power_cycle_off_s": 0.05}
        base.update(over)
        return base

    def stop(self) -> None:
        self._stop.set()
        self.sock.close()
        self.thread.join(timeout=5)


@pytest.fixture
def fake_kasa():
    dev = FakeKasa()
    yield dev
    dev.stop()


def _dead_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# -------------------------------------------------------- KasaPlug (Batch B)


def test_relay_state_reads_relay(fake_kasa):
    fake_kasa.relay = 1
    plug = power_plug.KasaPlug("127.0.0.1", port=fake_kasa.port)
    assert plug.relay_state() == 1


def test_set_relay_off_is_verified(fake_kasa):
    fake_kasa.relay = 1
    plug = power_plug.KasaPlug("127.0.0.1", port=fake_kasa.port)
    assert plug.set_relay(0) == 0
    assert fake_kasa.transitions == [0]


def test_set_relay_on_is_verified(fake_kasa):
    fake_kasa.relay = 0
    plug = power_plug.KasaPlug("127.0.0.1", port=fake_kasa.port)
    assert plug.set_relay(1) == 1
    assert fake_kasa.transitions == [1]


def test_plug_unreachable_raises():
    plug = power_plug.KasaPlug("127.0.0.1", port=_dead_port(), timeout=2.0)
    with pytest.raises(power_plug.PowerPlugError):
        plug.relay_state()


def test_plug_hang_times_out():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_stall():
        conn, _ = listener.accept()  # accept, never answer, hold
        time.sleep(3)
        conn.close()

    threading.Thread(target=accept_and_stall, daemon=True).start()
    plug = power_plug.KasaPlug("127.0.0.1", port=port, timeout=0.3)
    with pytest.raises(power_plug.PowerPlugError):
        plug.relay_state()


# ------------------------------------------------- actions layer (Batch C)
# read_state / set_relay_state / cycle — the shared surface used by BOTH the
# CLI one-shots and the HTTP endpoint. Degrade gracefully (ok: False) instead
# of raising when the plug is unconfigured or unreachable.


def test_read_state_on(fake_kasa):
    fake_kasa.relay = 1
    assert power_plug.read_state(fake_kasa.cfg()) == {"ok": True, "state": "on"}


def test_read_state_off(fake_kasa):
    fake_kasa.relay = 0
    assert power_plug.read_state(fake_kasa.cfg()) == {"ok": True, "state": "off"}


def test_read_state_no_ip_degrades():
    res = power_plug.read_state({"power_plug_ip": ""})
    assert res["ok"] is False
    assert res["state"] == "unknown"


def test_read_state_plug_down_degrades():
    res = power_plug.read_state({"power_plug_ip": "127.0.0.1",
                                 "power_plug_port": _dead_port()})
    assert res["ok"] is False
    assert res["state"] == "unknown"


def test_set_relay_on_is_verified(fake_kasa):
    fake_kasa.relay = 0
    assert power_plug.set_relay_state(fake_kasa.cfg(), on=True) == \
        {"ok": True, "state": "on"}
    assert fake_kasa.transitions == [1]


def test_set_relay_off_is_verified(fake_kasa):
    fake_kasa.relay = 1
    assert power_plug.set_relay_state(fake_kasa.cfg(), on=False) == \
        {"ok": True, "state": "off"}
    assert fake_kasa.transitions == [0]


def test_set_relay_no_ip_degrades():
    res = power_plug.set_relay_state({"power_plug_ip": ""}, on=True)
    assert res["ok"] is False
    assert res["state"] == "unknown"


def test_cycle_off_then_on(fake_kasa):
    fake_kasa.relay = 1
    cfg = fake_kasa.cfg(power_cycle_off_s=12.5)
    slept: list[float] = []
    res = power_plug.cycle(cfg, sleep=slept.append)
    assert res == {"ok": True, "state": "on"}
    assert slept == [12.5]           # used the configured off duration
    assert fake_kasa.transitions == [0, 1]   # off first, then on


def test_cycle_no_ip_degrades_without_sleeping():
    slept: list[float] = []
    res = power_plug.cycle({"power_plug_ip": ""}, sleep=slept.append)
    assert res["ok"] is False
    assert res["state"] == "unknown"
    assert slept == []


def test_cycle_mid_failure_reports_actual_off_state(fake_kasa):
    # cycle opens: set(0)=conn1, get-verify=conn2, [sleep], set(1)=conn3.
    # Drop conn3 (the "on" step): the relay is genuinely left OFF, and the
    # result MUST say "off" — never claim a cycle that didn't complete.
    fake_kasa.relay = 1
    fake_kasa.drop_on_connection = 3
    res = power_plug.cycle(fake_kasa.cfg(power_cycle_off_s=0.0),
                           sleep=lambda s: None)
    assert res["ok"] is False
    assert res["state"] == "off"
    assert fake_kasa.transitions == [0]   # only the "off" ever applied


# -------------------------------------------------------- HTTP (Batch D)
# Real in-process ThreadingHTTPServer on an ephemeral port, driven over real
# sockets with urllib — no handler mocks. Contract: 200 + JSON when the
# action completed (ok in the body), 502 + JSON when the plug side failed,
# 404 unknown path, 405 wrong method.


def _req(method: str, url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def endpoint(fake_kasa):
    server = power_plug.build_server(fake_kasa.cfg(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_http_state(endpoint, fake_kasa):
    fake_kasa.relay = 1
    code, body = _req("GET", endpoint + "/state")
    assert code == 200
    assert body == {"ok": True, "state": "on"}


def test_http_off(endpoint, fake_kasa):
    fake_kasa.relay = 1
    code, body = _req("POST", endpoint + "/off")
    assert code == 200
    assert body == {"ok": True, "state": "off"}
    assert fake_kasa.transitions == [0]


def test_http_on(endpoint, fake_kasa):
    fake_kasa.relay = 0
    code, body = _req("POST", endpoint + "/on")
    assert code == 200
    assert body == {"ok": True, "state": "on"}
    assert fake_kasa.transitions == [1]


def test_http_cycle(endpoint, fake_kasa):
    fake_kasa.relay = 1
    code, body = _req("POST", endpoint + "/cycle")
    assert code == 200
    assert body == {"ok": True, "state": "on"}
    assert fake_kasa.transitions == [0, 1]


def _down_endpoint():
    """An endpoint whose configured plug is unreachable."""
    server = power_plug.build_server(
        {"power_plug_ip": "127.0.0.1", "power_plug_port": _dead_port()},
        "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_state_plug_down_is_502():
    gen = _down_endpoint()
    base = next(gen)
    try:
        code, body = _req("GET", base + "/state")
    finally:
        gen.close()
    assert code == 502
    assert body["ok"] is False
    assert body["state"] == "unknown"


def test_http_cycle_plug_down_is_502():
    gen = _down_endpoint()
    base = next(gen)
    try:
        code, body = _req("POST", base + "/cycle")
    finally:
        gen.close()
    assert code == 502
    assert body["ok"] is False


def test_http_unknown_path_is_404(endpoint):
    code, body = _req("GET", endpoint + "/nope")
    assert code == 404


def test_http_wrong_method_is_405(endpoint):
    assert _req("GET", endpoint + "/on")[0] == 405
    assert _req("POST", endpoint + "/state")[0] == 405


# ---------------------------------------------------------- CLI (Batch E)
# The one-shots are also the manual test tools: print one JSON line, exit 0
# on ok / 1 on degradation (so scripts and the watchdog can rely on both).


def _run_cli(capsys, *argv) -> tuple[int, dict]:
    from cc_continuity import cli
    rc = cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_state(fake_kasa, cc_home, capsys):
    fake_kasa.relay = 1
    (cc_home / "config.json").write_text(json.dumps(fake_kasa.cfg()))
    rc, body = _run_cli(capsys, "power-plug", "state")
    assert rc == 0
    assert body == {"ok": True, "state": "on"}


def test_cli_off(fake_kasa, cc_home, capsys):
    fake_kasa.relay = 1
    (cc_home / "config.json").write_text(json.dumps(fake_kasa.cfg()))
    rc, body = _run_cli(capsys, "power-plug", "off")
    assert rc == 0
    assert body == {"ok": True, "state": "off"}
    assert fake_kasa.transitions == [0]


def test_cli_cycle(fake_kasa, cc_home, capsys):
    fake_kasa.relay = 1
    (cc_home / "config.json").write_text(json.dumps(fake_kasa.cfg()))
    rc, body = _run_cli(capsys, "power-plug", "cycle")
    assert rc == 0
    assert body == {"ok": True, "state": "on"}
    assert fake_kasa.transitions == [0, 1]


def test_cli_state_unconfigured_exits_1(cc_home, capsys):
    (cc_home / "config.json").write_text(
        json.dumps({"power_plug_ip": ""}))
    rc, body = _run_cli(capsys, "power-plug", "state")
    assert rc == 1
    assert body == {"ok": False, "state": "unknown",
                    "error": "power_plug_ip not set in config"}


def test_cli_cycle_plug_down_exits_1(cc_home, capsys):
    (cc_home / "config.json").write_text(json.dumps(
        {"power_plug_ip": "127.0.0.1", "power_plug_port": _dead_port()}))
    rc, body = _run_cli(capsys, "power-plug", "cycle")
    assert rc == 1
    assert body["ok"] is False
    assert body["state"] == "unknown"


def test_cli_parser_accepts_all_actions():
    from cc_continuity import cli
    for action in ("state", "on", "off", "cycle", "serve", "install",
                   "uninstall", "start", "stop", "status"):
        args = cli.build_parser().parse_args(["power-plug", action])
        assert args.command == "power-plug"
        assert args.power_plug_command == action


# ----------------------------------------------------- plist (Batch F)


def test_plist_path(cc_home):
    assert power_plug.plist_path(cc_home) == \
        cc_home / "Library" / "LaunchAgents" / \
        "com.claude-continuity.power-plug.plist"


def test_plist_render_bakes_binary_and_serve(cc_home):
    xml = power_plug.plist_render("/opt/bin/cc-continuity", cc_home)
    assert "<string>com.claude-continuity.power-plug</string>" in xml
    assert "<string>/opt/bin/cc-continuity</string>" in xml
    assert "<string>power-plug</string>" in xml
    assert "<string>serve</string>" in xml
    assert "<key>RunAtLoad</key>\n  <true/>" in xml
    assert "<key>KeepAlive</key>\n  <true/>" in xml
    assert str(cc_home / "logs" / "power-plug.out") in xml
    assert str(cc_home / "logs" / "power-plug.err") in xml


class _FakeProc:
    returncode = 0
    stdout = b""

    def __init__(self, returncode: int = 0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


def test_install_plist_writes_and_boots(cc_home):
    calls: list[list[str]] = []

    def fake_runner(cmd, **kw):
        calls.append(cmd)
        return _FakeProc()

    p = power_plug.install_plist(cc_home, runner=fake_runner,
                                 which=lambda name: f"/fake/{name}")
    assert p == cc_home / "Library" / "LaunchAgents" / \
        "com.claude-continuity.power-plug.plist"
    assert p.is_file()
    assert "<string>/fake/cc-continuity</string>" in p.read_text()
    assert calls and calls[0][:2] == ["launchctl", "bootstrap"]
    assert calls[0][2] == f"gui/{__import__('os').getuid()}"
    assert calls[0][3] == str(p)


def test_install_plist_missing_binary_raises(cc_home):
    with pytest.raises(power_plug.PowerPlugError):
        power_plug.install_plist(cc_home, runner=lambda cmd, **kw: _FakeProc(),
                                 which=lambda name: None)


def test_uninstall_plist_boots_out_and_removes(cc_home):
    p = power_plug.plist_path(cc_home)
    p.parent.mkdir(parents=True)
    p.write_text("plist")
    calls: list[list[str]] = []

    def fake_runner(cmd, **kw):
        calls.append(cmd)
        return _FakeProc()

    power_plug.uninstall_plist(cc_home, runner=fake_runner)
    assert not p.exists()
    assert calls and calls[0][:2] == ["launchctl", "bootout"]
    assert calls[0][2] == (f"gui/{__import__('os').getuid()}"
                           "/com.claude-continuity.power-plug")


def test_cli_status_not_loaded(cc_home, capsys):
    import argparse
    from cc_continuity import cli
    args = argparse.Namespace(command="power-plug", power_plug_command="status")
    rc = cli.cmd_power_plug(args, home=cc_home,
                            runner=lambda cmd, **kw: _FakeProc(returncode=1))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not loaded" in out
