#!/usr/bin/env python3
"""Live acceptance for the waiting blink (issue #11) - no human eyes needed.

Drives the REAL stack end to end:

  real tmux session (main pane + progress pane spawned by the real
  ``ensure_progress_pane`` -> ``cc-continuity progress-pane @N`` = the
  deployed app-dir animator) + a REAL attached tmux client whose tty is a
  pty we capture with ``script``.

Then it sets/clears the gate through the real writer path (statusline's
``render_segment`` text -> ``progress_pane.write_progress_data``) and
asserts the four observable facts a human would watch:

  1. the tmux window name alternates WAIT <-> original at the cadence
  2. the OSC 6 fill bytes reach the attached client's tty (capture)
  3. the OSC 6 RESET reaches it too, and the name returns on clear
  4. ``blink.log`` recorded the start and the restore

The statusline-to-text link (BLOCKED/ASK suffixes) is unit-pinned in
tests/test_statusline.py; this script pins the animator->Blinker->tmux/tty
live path. Exit 0 on PASS, 1 with diagnostics on any failed assertion.

Usage: uv run python tools/e2e_blink.py
"""

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cc_continuity import blink, config, paths, progress_pane, statusline

BIN = "tmux"


def tmux(*args: str) -> str | None:
    """One tmux call: stripped stdout, None on failure."""
    try:
        proc = subprocess.run([BIN, *args], capture_output=True, timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


class E2E:
    def __init__(self, attempt: int):
        self.attempt = attempt
        self.sess = f"blink-e2e-{os.getpid()}-{attempt}"
        self.capture = Path(f"/tmp/blink-e2e-{os.getpid()}-{attempt}.log")
        self.client: subprocess.Popen | None = None
        self.session_created = False
        self.win: str | None = None

    # -- plumbing ---------------------------------------------------------

    def cleanup(self) -> None:
        if self.client is not None and self.client.poll() is None:
            self.client.terminate()
            try:
                self.client.wait(timeout=3)
            except Exception:
                self.client.kill()
        if self.session_created:
            tmux("kill-session", "-t", self.sess)
        try:
            self.capture.unlink()
        except OSError:
            pass

    def fail(self, msg: str, extra: dict | None = None) -> None:
        print(f"FAIL (attempt {self.attempt}): {msg}")
        print("--- diagnostics ---")
        try:
            target = self.win or f"{self.sess}:demo"
            names = tmux("display-message", "-p", "-t", target,
                         "#{window_name}")
            print(f"window_name_now={names!r}")
            print(f"list_clients={tmux('list-clients', '-t', self.sess)!r}")
            print(f"list_panes={tmux('list-panes', '-t', self.sess)!r}")
        except Exception as exc:  # diagnostics must never mask the failure
            print(f"diagnostics error: {exc}")
        try:
            data = self.capture.read_bytes()
            print(f"capture_bytes={len(data)} tail={data[-400:]!r}")
        except OSError as exc:
            print(f"capture unreadable: {exc}")
        try:
            log = paths.logs_dir() / "blink.log"
            print(f"blink.log_tail={log.read_text()[-600:]!r}")
        except OSError as exc:
            print(f"blink.log unreadable: {exc}")
        for key, value in (extra or {}).items():
            print(f"{key}={value!r}")
        self.cleanup()
        sys.exit(1)

    def ok(self, msg: str) -> None:
        print(f"ok: {msg}")

    # -- phases -----------------------------------------------------------

    def spawn_stack(self) -> str:
        if tmux("new-session", "-d", "-s", self.sess, "-n", "demo",
                "-c", os.environ["HOME"], "sleep", "600") is None:
            self.fail("could not create the test tmux session")
        self.session_created = True
        main_pane = tmux("display-message", "-p", "-t", f"{self.sess}:demo",
                         "#{pane_id}")
        if not main_pane:
            self.fail("test window did not come up")
        os.environ["TMUX_PANE"] = main_pane
        win = progress_pane.resolve_window_id()
        if not win:
            self.fail("resolve_window_id failed on the test window")
        self.win = win
        # the real production spawn path (same CLI the status line uses)
        res = progress_pane.ensure_progress_pane(config.load_config())
        if not res.get("created") and res.get("reason") != "alive":
            self.fail(f"ensure_progress_pane refused: {res}")
        # a REAL attached client, on a pty we capture: Blinker's bytes to
        # this tty are exactly what an iTerm2 tab would receive
        env = dict(os.environ, TERM="xterm-256color")
        self.client = subprocess.Popen(
            ["script", "-q", str(self.capture), BIN, "attach", "-t", self.sess],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            out = tmux("list-clients", "-t", win, "-F", "#{client_tty}") or ""
            ttys = [line for line in out.splitlines()
                    if line.strip() and line.strip() != "not a tty"]
            if ttys:
                self.ok(f"stack up: window={win} client_tty={ttys}")
                return win
            time.sleep(0.1)
        self.fail("attached client never presented a tty")
        raise AssertionError  # unreachable

    def poll_names(self, seconds: float) -> list[str]:
        # target the stable window id: the NAME is the very thing under
        # test (WAIT rotations would make a "sess:demo" target miss)
        names: list[str] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            n = tmux("display-message", "-p", "-t", self.win,
                     "#{window_name}")
            if n is not None:
                names.append(n)
            time.sleep(0.05)
        return names

    def run(self) -> None:
        win = self.spawn_stack()
        log_path = paths.logs_dir() / "blink.log"
        log_offset = log_path.stat().st_size if log_path.exists() else 0

        # gate ON: the real statusline text (BLOCKED + ASK) via the real
        # writer helper; animator reloads on mtime change
        gate = statusline.render_segment(0.5, None, True,
                                         blocked="unconsumed needs_human",
                                         ansi=False, ask=True)
        if "BLOCKED" not in gate or "ASK" not in gate:
            self.fail(f"render_segment lost the markers: {gate!r}")
        progress_pane.write_progress_data(win, gate)
        names = self.poll_names(3.0)
        seen_wait = [n for n in names if n.startswith("WAIT")]
        seen_orig = [n for n in names if n == "demo"]
        capture = self.capture.read_bytes() if self.capture.exists() else b""
        fill = blink.osc6_fill(blink.BLACK_FILL)
        if not seen_wait:
            self.fail("window name never rotated to WAIT", {"names": names})
        self.ok(f"window name rotated: {seen_wait[0]!r} ({len(seen_wait)} ticks)")
        if not seen_orig:
            self.fail("original name never returned between flips",
                      {"names": names})
        self.ok("window name alternated back to the original")
        if fill not in capture:
            self.fail("OSC 6 fill bytes never reached the client tty")
        self.ok("OSC 6 fill bytes reached the attached client's tty")

        # gate OFF: same writer path, no markers -> restore
        clear = statusline.render_segment(0.5, None, True, ansi=False)
        if "BLOCKED" in clear or "ASK" in clear:
            self.fail(f"clear text still carries a marker: {clear!r}")
        pre_clear = len(self.capture.read_bytes())
        progress_pane.write_progress_data(win, clear)
        tail_names = self.poll_names(1.5)
        # the FIRST samples may still show WAIT: the animator reloads the
        # text on the next mtime tick after the write. The real pin is
        # settling: from the first "demo" on, no WAIT may ever return.
        if not tail_names or tail_names[-1] != "demo" or "demo" not in tail_names:
            self.fail("name did not settle back to the original after clear",
                      {"tail_names": tail_names})
        settled = tail_names[tail_names.index("demo"):]
        if any(n.startswith("WAIT") for n in settled):
            self.fail("window re-blinked after a clean clear",
                      {"settled": settled})
        self.ok("name settled on the original after clear")
        # the client tty also streams tmux redraws forever, so search the
        # post-clear window only (a stale RESET from the flip phases must
        # not satisfy this)
        post_clear = self.capture.read_bytes()[pre_clear:]
        if blink.OSC6_RESET not in post_clear:
            self.fail("OSC 6 RESET never reached the client tty after clear")
        self.ok("OSC 6 RESET reached the attached client's tty on restore")

        try:
            new_lines = log_path.read_bytes()[log_offset:].decode(
                "utf-8", "replace")
        except OSError as exc:
            self.fail(f"blink.log unreadable: {exc}")
        if "blink start" not in new_lines or "blink restore" not in new_lines:
            self.fail("blink.log missing start/restore lines",
                      {"new_lines": new_lines})
        self.ok("blink.log recorded start and restore")

        self.cleanup()


def main() -> int:
    cfg = config.load_config()
    if not config.blink_enabled(cfg):
        print("FAIL: config has blink disabled - the acceptance drives the "
              "live animator, which needs the default (enabled)")
        return 1
    for attempt in (1, 2):
        e2e = E2E(attempt)
        atexit.register(e2e.cleanup)
        try:
            e2e.run()
        except SystemExit as exc:  # fail() already printed diagnostics
            if exc.code == 0:
                return 0
            if attempt == 2:
                return 1
            print("retrying once with a fresh session...")
            continue
        except Exception as exc:
            e2e.fail(f"unexpected error: {exc!r}")
        atexit.unregister(e2e.cleanup)
        e2e.cleanup()
        print("E2E BLINK ACCEPTANCE: PASS")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
