#!/usr/bin/env bash
# cc-continuity WSL e2e — the plan's Task 9 gate (spec §11).
#
# Runs INSIDE the Ubuntu distro, on a FRESH VM (the fresh-VM install
# path is what this validates). It uses the distro's REAL user home
# (throwaway VM) so the Windows-side task, the rc self-heal and the
# daemon sessions all live in the real user context.
#
# Phases (a PASS line per phase; the first FAIL exits nonzero):
#   1  prereqs (WSL, tmux >= 3.2, claude, python >= 3.11, dev tree)
#   2  real `cc-continuity install --window 262144` + doctor green
#   3  configure: ntfy test topic + watchdog enabled (+ doctor test push)
#   4  `watchdog install` + `watchdog digest install` (TmuxHost on WSL1)
#   5  a shortened 5-session rotation chain (headless, unattended)
#   6  resilience: killed session -> `daemon ensure` recreates it;
#      killed child -> supervisor respawns (new pid in the state file)
#   7  Windows side: E2E_WINDOWS=1 creates the ONLOGON task (spec §7.5)
#      and prints the `wsl --shutdown` steps; E2E_WINDOWS=2 (after the
#      shutdown + restart, run from a NEW WSL shell) asserts the daemon
#      sessions are restored (rc self-heal or the logon task), then
#      re-runs phases 8-9
#   8  ntfy pushes received on the test topic
#   9  final `cc-continuity doctor` green
#
# Privacy-clean by construction (same rules as tools/demo_gif.sh): the
# committed file carries no personal data — the fake project "e2e-app"
# uses the git identity "Demo User <demo@example.com>"; the model
# endpoint comes from ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY in the
# calling shell, never from this file.
#
# E2E caveats (a disposable VM makes these safe):
#  - phase 5 tunes the real config.json (window_tokens=E2E_WINDOW,
#    unattended=true) so the chain rotates on a real model in bounded
#    time; `cc-continuity configure --window 262144` restores it after.
#  - phase 7's task uses the ABSOLUTE launcher path — a non-interactive
#    `wsl.exe -d <distro> <cmd>` does not source the shell rc, so
#    ~/.local/bin is not guaranteed on its PATH.
#
# Usage (inside the distro, from the dev-tree clone):
#   tools/e2e_wsl.sh                # phases 1-6, 8, 9
#   E2E_WINDOWS=1 tools/e2e_wsl.sh  # + phase 7 pre (create the task)
#   E2E_WINDOWS=2 tools/e2e_wsl.sh  # phase 7 post + 8, 9 — after
#                                   # `wsl --shutdown` + restart, from a
#                                   # NEW WSL shell (its rc self-heal
#                                   # has already run)
#
# Env: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY required for the full run
#      (the model box over the network); optional ANTHROPIC_MODEL,
#      E2E_WATCHDOG_HEALTH (default: ANTHROPIC_BASE_URL with a
#      trailing /v1 stripped, plus /health).
#      E2E_WINDOW (default 64000 — the demo's measured sizing),
#      E2E_CHAIN_TIMEOUT (default 10800 — an ARM VM + network model is
#      slow; this is a correctness gate, not a performance gate),
#      E2E_NTFY (default https://ntfy.sh), E2E_NTFY_TOPIC (default
#      cc-continuity-e2e-wsl), E2E_DISTRO (default $WSL_DISTRO_NAME).
set -euo pipefail

DEV_TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STORE="$HOME/.claude-continuity"
PROJ="e2e-app"
E2E_PROJ="$HOME/e2e-app"
TMUX_S="cc-e2e"
E2E_WINDOW="${E2E_WINDOW:-64000}"
CHAIN_TIMEOUT="${E2E_CHAIN_TIMEOUT:-10800}"
E2E_NTFY="${E2E_NTFY:-https://ntfy.sh}"
E2E_NTFY_TOPIC="${E2E_NTFY_TOPIC:-cc-continuity-e2e-wsl}"
DISTRO="${E2E_DISTRO:-${WSL_DISTRO_NAME:-Ubuntu}}"
E2E_WINDOWS="${E2E_WINDOWS:-0}"

step=0
pass() { step=$((step + 1)); printf '\033[1;32mPASS\033[0m [%02d] %s\n' "$step" "$1"; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$1" >&2; exit 1; }
phase() { printf '\n\033[1;36m=== PHASE: %s ===\033[0m\n' "$1"; }

# --- helpers -----------------------------------------------------------------

proj_sessions() {  # $1=nnn -> 0 when sessions/$1/state.json exists
  local d
  d="$(ls -d "$STORE"/projects/*/sessions 2>/dev/null | head -1 || true)"
  [[ -f "$d/$1/state.json" ]]
}

proj_state() {  # $1=nnn -> the state value ("" if missing)
  local d
  d="$(ls -d "$STORE"/projects/*/sessions 2>/dev/null | head -1 || true)"
  python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("state", ""))
except Exception:
    pass' "$d/$1/state.json" 2>/dev/null || true
}

turn_settled() {  # $1=nnn -> 0 when the turn ended and the pane is idle
  local d
  d="$(ls -d "$STORE"/projects/*/sessions 2>/dev/null | head -1 || true)"
  python3 -c '
import json, sys, time
try:
    s = json.load(open(sys.argv[1]))
    ok = (not s.get("turn_active")) and (time.time() - (s.get("last_stop_ts") or 0) < 90)
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)' "$d/$1/state.json" 2>/dev/null || return 1
}

latest_session() {  # -> the highest existing session number (0 if none)
  local d n max=0
  d="$(ls -d "$STORE"/projects/*/sessions 2>/dev/null | head -1 || true)"
  [[ -n "$d" ]] || { echo 0; return; }
  for n in "$d"/*/; do
    n="$(basename "$n")"
    [[ "$n" =~ ^[0-9]+$ ]] && (( 10#$n > max )) && max=$((10#$n))
  done
  echo "$max"
}

doctor_green() {  # exit 0 iff doctor reports no failing check
  set +e
  cc-continuity doctor
  local rc=$?
  set -e
  (( rc == 0 ))
}

read_child_pid() {  # -> the watchdog state file's child_pid ("" if missing)
  python3 -c '
import json
try:
    v = json.load(open("'$STORE'/runtime/daemon-watchdog.json")).get("child_pid")
    print("" if v is None else v)
except Exception:
    pass' 2>/dev/null || true
}

# --- phases ------------------------------------------------------------------

preflight() {
  phase "1 prereqs"
  [[ "$(uname -s)" == "Linux" ]] || fail "not Linux (run this inside the distro)"
  grep -qi microsoft /proc/version || fail "not a WSL distro (no Microsoft in /proc/version)"
  command -v tmux    >/dev/null || fail "tmux not on PATH (sudo apt install tmux)"
  command -v claude  >/dev/null || fail "claude not on PATH (run the Linux installer)"
  command -v curl    >/dev/null || fail "curl not on PATH"
  local tmux_v py_v claude_v
  tmux_v="$(tmux -V | grep -oE '[0-9]+\.[0-9]+' | head -1)"
  python3 -c "import sys; t = tuple(int(x) for x in sys.argv[1].split('.')); sys.exit(0 if t >= (3, 2) else 1)" "$tmux_v" \
    || fail "tmux $tmux_v < 3.2 (the daemon session uses new-session -e)"
  py_v="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "python3 $py_v < 3.11 (Ubuntu 24.04, or deadsnakes python3.11)"
  claude_v="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  python3 -c 'import sys, re
m = re.match(r"(\d+)\.(\d+)\.(\d+)", sys.argv[1])
v = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
sys.exit(0 if v >= (2, 1, 209) else 1)' "$claude_v" || fail "claude $claude_v < 2.1.209"
  [[ -d "$DEV_TREE/cc_continuity" ]] || fail "not the dev tree: $DEV_TREE"
  if [[ "$E2E_WINDOWS" != "2" ]]; then
    for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY; do
      eval "val=\${$v:-}"
      [[ -n "$val" ]] || fail "$v not set in this shell"
      case "$val" in *\'*) fail "$v contains a single quote";; esac
    done
    # A hung model server wastes the whole run — probe it up front.
    local health="${E2E_WATCHDOG_HEALTH:-${ANTHROPIC_BASE_URL%/v1}/health}"
    curl -sf -m 30 "$health" >/dev/null \
      || curl -sf -m 90 "${ANTHROPIC_BASE_URL%/}/v1/chat/completions" \
        -H "Content-Type: application/json" -H "x-api-key: ${ANTHROPIC_API_KEY}" \
        -d "{\"model\":\"${ANTHROPIC_MODEL}\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with: go\"}]}" >/dev/null \
      || fail "model endpoint did not answer a probe in 90s — check it and re-run"
  fi
  pass "prereqs (WSL, tmux $tmux_v, claude $claude_v, python $py_v)"
}

install_real() {
  phase "2 install + doctor"
  ( cd "$DEV_TREE" && python3 -m cc_continuity.cli install --window 262144 --ntfy off ) \
    || fail "cc-continuity install failed"
  export PATH="$HOME/.local/bin:$PATH"
  command -v cc-continuity >/dev/null || fail "launcher missing after install"
  doctor_green || fail "doctor not green right after install"
  pass "install --window 262144 + doctor green"
}

configure_real() {
  phase "3 configure (ntfy + watchdog)"
  cc-continuity configure --ntfy "$E2E_NTFY/$E2E_NTFY_TOPIC" \
    || fail "configure --ntfy failed"
  local health="${E2E_WATCHDOG_HEALTH:-${ANTHROPIC_BASE_URL%/v1}/health}"
  cc-continuity configure --watchdog-enabled true --watchdog-health "$health" \
    || fail "configure --watchdog-* failed"
  # This doctor sends the first ntfy test push (phase 8 counts it).
  doctor_green || fail "doctor not green after configure"
  pass "configure ntfy topic + watchdog enabled (+ test push sent)"
}

watchdog_install() {
  phase "4 watchdog + digest install (TmuxHost)"
  cc-continuity watchdog install    || fail "watchdog install failed"
  cc-continuity watchdog digest install || fail "watchdog digest install failed"
  for n in watchdog watchdog-digest; do
    [[ -f "$STORE/runtime/daemon-$n.json" ]] || fail "state file missing for $n"
    tmux has-session -t "cc-d-$n" 2>/dev/null || fail "session cc-d-$n not running"
  done
  # Full-line patterns (rc_daemon_line writes each line verbatim, ending
  # at the session name — the digest line's longer name keeps the two
  # patterns unambiguous without a trailing-space trick).
  grep -q "tmux has-session -t cc-d-watchdog 2>/dev/null || cc-continuity daemon ensure cc-d-watchdog" "$HOME/.bashrc" \
    || fail "rc self-heal line for watchdog missing from ~/.bashrc"
  grep -q "tmux has-session -t cc-d-watchdog-digest 2>/dev/null || cc-continuity daemon ensure cc-d-watchdog-digest" "$HOME/.bashrc" \
    || fail "rc self-heal line for watchdog-digest missing from ~/.bashrc"
  pass "both daemons: state files + tmux sessions + rc self-heal lines"
}

input_box_up() {  # $1=pane text -> 0 when the idle input box is visible
  # The live input box: a col-0 "❯" line — empty, or carrying a rotating
  # placeholder hint ('Try "write a test for <filepath>"') — with "─"
  # divider lines on both sides (box chrome; the header divider carries
  # the session name). Scrollback user messages also start with ❯, but
  # the e2e only probes while the pane is fresh, before the first paste.
  printf '%s\n' "$1" | awk '
    /^[─]/ && prev ~ /^❯/ && above ~ /^[─]/ { found = 1 }
    { above = prev; prev = $0 }
    END { exit(found ? 0 : 1) }'
}

chain_task() {
  # Sizing for the thinking-OFF regime (measured live, qwen3.8-27b via the
  # LAN relay, 64K window): the 8-part task grew one session from a ~30K
  # baseline to 59% in a single 12.5-min turn (~0.97K context per part) —
  # too little to cross the 62% auto-rotation threshold at all. Each
  # rotated successor starts at ~33-35K (baseline + handoff) and needs
  # ~9K more work to cross. 48 parts ~= 50-60K of work (later parts grow
  # more: every "run the full suite" prints a bigger suite) -> 4 crossings
  # -> a 5-6 session chain. If the model's static baseline differs, tune
  # E2E_WINDOW and/or this task (assert: 005 exists, 001-004 ROTATED —
  # extra sessions are OK).
  cat <<'EOF'
Build a small CLI tool in this repo. Work autonomously; do not ask questions; complete the parts in order, as far as this session gets (if the session rotates before the last part, the next session continues from the handoff). Write all output (chat text, code, comments, commit message) in English only. Do not run git commit before the final part.
Part 1: create wordcount.py — a Python CLI that takes a file path and prints its line, word and character counts (like wc -lwc). Add test_wordcount.py using only the standard library (unittest) with at least 4 tests. Run the tests until they pass.
Part 2: add a --json flag that prints the counts as a JSON object. Add at least 2 tests, then run the full suite.
Part 3: add a --csv flag that prints a header row followed by one data row. Add at least 2 tests, then run the full suite.
Part 4: add a --limit N option that counts only the first N lines (must work with plain, --json and --csv). Add at least 2 tests, run the full suite.
Part 5: add a --sort flag (alpha order of keys for --json, column order for --csv, stable plain output). Add at least 2 tests, run the full suite.
Part 6: add a --files f1 f2 … option (multiple inputs, per-file rows plus a TOTAL row; plain and --csv). Add at least 3 tests, run the full suite.
Part 7: add a --errors file option (writes undecodable lines to the named file as path:line) plus at least 2 tests; run the full suite.
Part 8: add --strip: remove leading and trailing whitespace from each line before counting. Add at least 2 tests, run the full suite.
Part 9: add --skip-blank: exclude blank or whitespace-only lines from all counts. Add at least 2 tests, run the full suite.
Part 10: add --min-line-length N: count only lines longer than N characters (applied before --strip). Add at least 2 tests, run the full suite.
Part 11: add --pattern RE: count only lines matching the regex; an invalid regex is a clear error with exit code 2. Add at least 3 tests, run the full suite.
Part 12: add --invert: count the lines that do NOT match --pattern; using it without --pattern is an error (exit code 2). Add at least 2 tests, run the full suite.
Part 13: add --top N: print the N most frequent words (case-insensitive) with their counts, below the main counts in plain mode. Add at least 3 tests, run the full suite.
Part 14: add --unique: count distinct words (case-sensitive); add a "unique" field to every output mode. Add at least 2 tests, run the full suite.
Part 15: add --avg: report average line length and average words per line; add avg_chars and avg_words fields to every output mode. Add at least 2 tests, run the full suite.
Part 16: add --range A:B: count only lines A through B (1-based, inclusive); A > B or A beyond the file end is exit code 2. Add at least 3 tests, run the full suite.
Part 17: add --tail N: count only the last N lines; combining --tail with --limit is exit code 2. Add at least 2 tests, run the full suite.
Part 18: add --json-indent N: pretty-print --json output with the given indentation. Add at least 2 tests, run the full suite.
Part 19: add --csv-delimiter C: use the single character C as the --csv delimiter (default comma). Add at least 2 tests, run the full suite.
Part 20: add --output FILE: write the result to FILE instead of stdout, creating parent directories as needed. Add at least 2 tests, run the full suite.
Part 21: add --max-lines N, --max-words N and --max-chars N: exit code 3 when any count exceeds its limit (CI-gate mode); they combine. Add at least 3 tests, run the full suite.
Part 22: support the path "-" to read from stdin. Add at least 2 tests, run the full suite.
Part 23: add --version: print "wordcount 1.0.0" and exit 0. Add at least 2 tests, run the full suite.
Part 24: rebuild the --help text: group the flags under Counting / Output / Gates, one line per flag, plus an Examples section. Add at least 1 test that --help exits 0 and lists all three group headers, then run the full suite.
Part 25: add --summary: one human line, e.g. "42 lines, 510 words, 3021 chars (avg 71.9 chars/line)". Add at least 2 tests, run the full suite.
Part 26: add --compare FILE2: count both files and print, per metric, the delta (FILE2 minus FILE1) with an explicit + or - sign; it must work in plain, --json and --csv. Add at least 3 tests, run the full suite.
Part 27: add --baseline FILE: store this run's counts as a JSON snapshot in FILE; if FILE already exists, print the delta against the stored snapshot and then update the file. Add at least 3 tests, run the full suite.
Part 28: add --count-matches: with --pattern, count total regex matches instead of matching lines; without --pattern it is exit code 2. Add at least 2 tests, run the full suite.
Part 29: add --min-word-length N: for --top, ignore words shorter than N characters. Add at least 2 tests, run the full suite.
Part 30: add --stopwords FILE: for --top, ignore the words listed one per line in FILE. Add at least 2 tests, run the full suite.
Part 31: add --sort-by count|alpha: for --top, order by frequency (ties alphabetical) or alphabetically. Add at least 2 tests, run the full suite.
Part 32: add --histogram: print line-length buckets (0-9, 10-19, 20-29, ..., 100+) with counts, below the main counts in plain mode. Add at least 2 tests, run the full suite.
Part 33: add --lines-only, --words-only and --chars-only: print a single number and nothing else; combining two of them is exit code 2. Add at least 3 tests, run the full suite.
Part 34: add --no-total: omit the TOTAL row in --files mode. Add at least 2 tests, run the full suite.
Part 35: add --label L: in --files mode, use L as the row label instead of each file path. Add at least 2 tests, run the full suite.
Part 36: extend the --errors format to path:line:code, where code names the decode error kind (such as invalid start byte, invalid continuation byte, truncated data, code point out of range). Add at least 2 tests, run the full suite.
Part 37: add --encoding ENC: open the input with the given encoding (default utf-8). Add at least 2 tests, run the full suite.
Part 38: add --crlf: treat \r\n as one newline and exclude \r from the character count (the default counts \r). Add at least 2 tests, run the full suite.
Part 39: add --tab-as N: count each tab as N spaces in the character count only (default 0 = a tab counts as one character). Add at least 2 tests, run the full suite.
Part 40: add --quiet: print nothing to stdout (the exit code still reflects the --max-* gates). Add at least 2 tests, run the full suite.
Part 41: make --compare work with --json: emit a JSON object holding both file objects and a diff object. Add at least 2 tests, run the full suite.
Part 42: make --files work with --top N: a per-file top-word list plus a merged TOTAL top list. Add at least 2 tests, run the full suite.
Part 43: polish every usage error (bad flag value, conflicting flags, missing file, bad --range, bad --csv-delimiter, unknown flag): print "wordcount: <reason>" to stderr and exit 2. Add at least 3 tests covering different cases, asserting both the message prefix and the exit code, then run the full suite.
Part 44: add --selftest: run built-in canned strings through the core flags (--json, --csv, --top, --range, --pattern) and print "selftest OK" on success. Add at least 2 tests, run the full suite.
Part 45: add a performance test: generate a 200,000-line file inside the test, assert that plain mode and --json each finish in under 10 seconds, and delete the file afterwards. Run the full suite.
Part 46: add pyproject.toml with [project] metadata (name, version, description, requires-python) and a [project.scripts] entry wordcount = "wordcount:main" (add a main() entry point if the script does not have one yet); add a test that tomllib parses the file and the fields exist. Do not pip install it. Run the full suite.
Part 47: add --list-flags: print every flag one per line as "flag — one-line description". Add at least 2 tests, run the full suite.
Part 48: rewrite README.md: the complete flag reference grouped like --help, one example per group, the exit-code table, and a CI-gate example. Run the full suite one final time, then git add -A and make a single git commit with a clear message listing the major features.
EOF
}

rotation_chain() {
  phase "5 rotation chain (5 sessions, headless)"
  # E2E tuning of the REAL config (a disposable VM): a small window so
  # AUTO rotation fires on the real model in bounded time, unattended so
  # the headless pane never waits on approval (the product's real
  # unattended mode — the dev loop runs it this way).
  python3 - "$STORE/config.json" "$E2E_WINDOW" <<'EOF'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["unattended"] = True
cfg["window_tokens"] = int(sys.argv[2])
with open(p, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"e2e tuning: unattended=true, window_tokens={cfg['window_tokens']}")
EOF
  # Keep the pane's scrollback generic (fresh-VM hygiene, demo rules).
  (
    cd "$E2E_PROJ"
    git init -q 2>/dev/null || true
    git config user.name "Demo User"
    git config user.email "demo@example.com"
    printf '# e2e-app\n\nThrowaway project for the cc-continuity WSL e2e.\n' > README.md
    git add README.md
    git commit -qm "initial commit" 2>/dev/null || true
  )
  # Pre-seed the first-run dialogs so the headless pane never stalls.
  local ver
  ver="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  python3 - "$HOME/.claude.json" "$ver" "$E2E_PROJ" <<'EOF'
import json, os, sys
p, ver, proj = sys.argv[1], sys.argv[2], os.path.realpath(sys.argv[3])
d = json.load(open(p)) if os.path.exists(p) else {}  # never-launched HOME
d["hasCompletedOnboarding"] = True
d["lastOnboardingVersion"] = ver
d["numStartups"] = d.get("numStartups", 0) + 1
d.setdefault("projects", {}).setdefault(proj, {})["hasTrustDialogAccepted"] = True
# 2.1.209 asks "Detected a custom API key… use it?" the first time an
# env key is used; customApiKeyResponses.approved stores key[-20:].
key = os.environ.get("ANTHROPIC_API_KEY", "")
if key:
    ca = d.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
    tail = key[-20:]
    approved = ca.setdefault("approved", [])
    if tail not in approved:
        approved.append(tail)
    ca["rejected"] = [r for r in ca.get("rejected", []) if r != tail]
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("onboarding + folder trust + API key pre-seeded")
EOF
  python3 - "$HOME/.claude/settings.json" <<'EOF'
import json, os, sys
p = sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p)) if os.path.exists(p) else {}  # never-launched HOME
d["skipDangerousModePermissionPrompt"] = True
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("settings: skipDangerousModePermissionPrompt=true")
EOF
  # Absolute claude path: the pane's shell is a bare non-interactive sh
  # (no rc, no claude() wrapper), and the `command` builtin wrapper works
  # only where /usr/bin/command exists (macOS) — not on the distro.
  local cmd claude_bin clean_path
  claude_bin="$(command -v claude)"
  # WSL interop appends space-bearing Windows entries to PATH
  # (/mnt/c/Program Files/...): an unquoted $PATH expansion in the
  # pane's shell word-splits them and `env` dies on the fragment
  # (rc 127). Strip such entries — the pane only needs
  # $HOME/.local/bin + the standard Linux dirs.
  clean_path="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v ' ' | paste -sd: -)"
  cmd="env PATH=\"$HOME/.local/bin:$clean_path\" LANG=C LC_ALL=C CC_CONTINUITY_NAME=$PROJ-001"
  cmd="$cmd ANTHROPIC_API_KEY='${ANTHROPIC_API_KEY}' ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL}'"
  [[ -n "${ANTHROPIC_MODEL:-}" ]] && cmd="$cmd ANTHROPIC_MODEL='${ANTHROPIC_MODEL}'"
  cmd="$cmd $claude_bin --dangerously-skip-permissions -n $PROJ-001"
  tmux kill-session -t "$TMUX_S" 2>/dev/null || true
  tmux new-session -d -s "$TMUX_S" -c "$E2E_PROJ" "$cmd"
  # Headless send: wait for the idle input box, paste the task, submit.
  local t start_t
  start_t="$(date +%s)"
  while :; do
    (( $(date +%s) - start_t > 300 )) && {
      tmux capture-pane -t "$TMUX_S" -p 2>/dev/null | tail -25 || true
      fail "pane not at the input box after 300s — check: tmux attach -t $TMUX_S"
    }
    t="$(tmux capture-pane -t "$TMUX_S" -p 2>/dev/null || true)"
    input_box_up "$t" && break
    sleep 2
  done
  tmux load-buffer -b e2etask - <<EOF
$(chain_task)
EOF
  tmux paste-buffer -b e2etask -p -d -t "$TMUX_S"
  tmux send-keys -t "$TMUX_S" Enter
  echo "task sent — the chain runs headless (attach: tmux attach -t $TMUX_S)"
  # Wait: 005 exists AND the latest session is settled (ROTATED is
  # terminal per session; the final one settles idle after its turn).
  local start now latest
  start="$(date +%s)"
  while :; do
    now="$(date +%s)"
    (( now - start > CHAIN_TIMEOUT )) && {
      tmux capture-pane -t "$TMUX_S" -p 2>/dev/null | tail -25 || true
      fail "chain did not reach a settled 5th session in ${CHAIN_TIMEOUT}s"
    }
    latest="$(latest_session)"
    # session dirs are zero-padded (paths.session_dir: {nnn:03d})
    if (( latest >= 5 )) && { proj_state "$(printf '%03d' "$latest")" | grep -q ROTATED || turn_settled "$(printf '%03d' "$latest")"; }; then
      # no newer session may appear while we check (settled == stable)
      sleep 30
      [[ "$(latest_session)" == "$latest" ]] && break
    fi
    sleep 20
  done
  local n
  for n in 1 2 3 4; do
    [[ "$(proj_state "$(printf '%03d' "$n")")" == "ROTATED" ]] \
      || fail "session 00$n is not ROTATED (state: $(proj_state "$(printf '%03d' "$n")"))"
  done
  echo "chain settled at session $(printf '%03d' "$latest") (attach: tmux attach -t $TMUX_S)"
  pass "rotation chain: 001-004 ROTATED, 00$latest settled"
}

resilience() {
  phase "6 resilience (ensure + supervisor respawn)"
  # 6a: kill the daemon session -> `daemon ensure` recreates it, and the
  # fresh supervisor records a new child in the state file.
  local pre new newer i
  pre="$(read_child_pid)"
  tmux kill-session -t cc-d-watchdog 2>/dev/null || true
  tmux has-session -t cc-d-watchdog 2>/dev/null && fail "session still alive after kill"
  cc-continuity daemon ensure watchdog || fail "daemon ensure failed"
  tmux has-session -t cc-d-watchdog 2>/dev/null \
    || fail "session cc-d-watchdog not recreated by daemon ensure"
  # The state file keeps the pre-kill pid until the fresh supervisor
  # records its child — wait for the change (a dead pid here would make
  # the 6b kill below exit under set -e).
  new=""
  for i in $(seq 1 15); do
    sleep 1
    new="$(read_child_pid)"
    [[ -n "$new" && "$new" != "$pre" ]] && break
  done
  [[ -n "$new" && "$new" != "$pre" ]] \
    || fail "fresh supervisor did not record a new child_pid (pre: $pre, now: ${new:-none})"
  # 6b: kill the supervised CHILD -> the supervisor respawns it (a new
  # child_pid lands in the state file; the session name is unchanged).
  kill -TERM "$new" 2>/dev/null \
    || fail "child $new already dead before the kill test (daemon unstable)"
  newer=""
  for i in $(seq 1 30); do
    sleep 2
    newer="$(read_child_pid)"
    [[ -n "${newer:-}" && "$newer" != "$new" ]] && break
  done
  [[ -n "${newer:-}" && "$newer" != "$new" ]] \
    || fail "supervisor did not respawn the child within 60s (pid $new)"
  tmux has-session -t cc-d-watchdog 2>/dev/null || fail "session gone after respawn"
  pass "ensure recreated the session; supervisor respawned the child (pid $new -> $newer)"
}

windows_side() {
  if [[ "$E2E_WINDOWS" == "1" ]]; then
    phase "7a windows: ONLOGON task"
    command -v schtasks.exe >/dev/null || fail "schtasks.exe not found (WSL interop off?)"
    local launcher
    launcher="$(command -v cc-continuity)"
    # Absolute path: a non-interactive `wsl.exe -d <distro> <cmd>` does
    # not source the shell rc, so ~/.local/bin is not on its PATH.
    schtasks.exe /Create /F /TN cc-continuity-ensure /SC ONLOGON \
      "/TR wsl.exe -d $DISTRO $launcher daemon ensure" \
      || fail "schtasks /Create failed"
    schtasks.exe /Query /TN cc-continuity-ensure >/dev/null \
      || fail "scheduled task not found after create"
    pass "ONLOGON task created + verified (distro: $DISTRO)"
    cat <<EOF
Next (operator, from PowerShell):
  1. wsl --shutdown
  2. wsl -d $DISTRO            (or just open a new WSL terminal — its
                               rc self-heal restores the daemons)
  3. in the NEW WSL shell:     E2E_WINDOWS=2 tools/e2e_wsl.sh
EOF
  elif [[ "$E2E_WINDOWS" == "2" ]]; then
    phase "7b windows: sessions restored after restart"
    local restored=0
    for n in watchdog watchdog-digest; do
      tmux has-session -t "cc-d-$n" 2>/dev/null && restored=$((restored + 1))
    done
    if (( restored < 2 )); then
      echo "not all sessions restored by the rc self-heal/task — running daemon ensure"
      cc-continuity daemon ensure || fail "daemon ensure failed after restart"
      for n in watchdog watchdog-digest; do
        tmux has-session -t "cc-d-$n" 2>/dev/null || fail "session cc-d-$n missing after ensure"
      done
    fi
    pass "daemon sessions restored after wsl --shutdown ($restored/2 pre-ensure)"
  fi
}

ntfy_received() {
  phase "8 ntfy pushes received"
  local events count
  events="$(curl -sf -m 30 "$E2E_NTFY/$E2E_NTFY_TOPIC?json=events" 2>/dev/null || echo '[]')"
  count="$(python3 - "$events" <<'EOF'
import json, sys
try:
    evs = json.loads(sys.argv[1])
except Exception:
    evs = []
n = sum(1 for e in evs
        if "cc-continuity" in (e.get("message", "") + e.get("title", "")))
print(n)
EOF
)"
  (( count >= 1 )) || fail "no cc-continuity events on topic $E2E_NTFY_TOPIC (got 0)"
  pass "ntfy: $count cc-continuity event(s) on $E2E_NTFY_TOPIC"
}

final_doctor() {
  phase "9 final doctor"
  doctor_green || fail "final doctor not green"
  pass "final doctor green"
}

# --- main ----------------------------------------------------------------------

mkdir -p "$E2E_PROJ"
if [[ "$E2E_WINDOWS" == "2" ]]; then
  # Post-restart run: install/config/chain already happened; the tmux
  # server was dead and is fresh — assert restoration, then the tail.
  export PATH="$HOME/.local/bin:$PATH"
  command -v cc-continuity >/dev/null || fail "cc-continuity not on PATH (run the full e2e first)"
  windows_side
  ntfy_received
  final_doctor
  echo
  printf '\033[1;32mE2E PASS\033[0m (%s) — all %s steps green\n' "post-restart" "$step"
else
  preflight
  install_real
  configure_real
  watchdog_install
  rotation_chain
  resilience
  windows_side
  ntfy_received
  final_doctor
  echo
  printf '\033[1;32mE2E PASS\033[0m (%s) — all %s steps green\n' "full run" "$step"
fi
