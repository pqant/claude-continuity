#!/usr/bin/env bash
# cc-continuity full-lifecycle demo — for the README GIF.
#
# Runs a REAL `cc-continuity install` + a real claude session (local model,
# from your ANTHROPIC_* env) inside a throwaway sandbox, and plays the
# whole lifecycle end-to-end:
#
#   install -> session demo-app-001 -> work -> context fills up ->
#   AUTO rotation (zero-touch) -> handoff + checkpoint -> /clear ->
#   demo-app-002 resumes from the handoff -> finishes the task
#
# Recording workflow:
#   1. tools/demo_gif.sh           (prints PHASE markers in this terminal)
#   2. tmux attach -t cc-demo      (the window you will record)
#   3. start screen recording (QuickTime / OBS; record the tmux window)
#   4. stop recording a few seconds after "DEMO COMPLETE"
#   5. tools/gif_from_video.sh rec.mov demo.gif
#
# Privacy-clean by construction: sandbox HOME, fake project "demo-app",
# git identity "Demo User", shell prompt user "demo", no personal paths.
# Run `--scan` before publishing (re-checks pane scrollback + sandbox
# against the personal-data battery).
#
# Usage:
#   tools/demo_gif.sh           full demo (needs ANTHROPIC_BASE_URL +
#                               ANTHROPIC_API_KEY in this shell)
#   tools/demo_gif.sh --dry     sandbox + install only (no model, no pane)
#   tools/demo_gif.sh --scan    privacy-scan the last run (pane + sandbox)
#   tools/demo_gif.sh --clean   kill the demo tmux session + delete sandbox
#
# Env overrides: CC_DEMO_SBX (default /tmp/cc-demo), CC_DEMO_WINDOW
# (rotation window in tokens; default 64000). Sized from measured runs
# (a small local model, window 50000): the first turn lands at 25.8K context
# (static prompt), every successor starts at 28.1K (static + ~2.3K
# resume bootstrap), the handoff read adds ~2.5K. For a SINGLE rotation
# at rotate_ratio 0.62 the successor must finish below the threshold:
# 28.1K + 2.5K + 0.25*T < 0.62*W, with 001 crossing at 75% of the task
# (T = (0.62*W - 25.8K)/0.75) that needs W > ~56K. 64000 leaves ~4K of
# margin: the task (4 parts, ~18.5K of work) makes 001 cross 0.62 at
# ~75% (the zero-touch moment) and 002 finish the last part + commit
# at ~0.55 — no second rotation (there is no loop guard in the product).
# Measured failure modes: W=50000 + old 2-part task — successors start
# at 0.56, only 3K under the threshold, and chain (run 6: four
# rotations). W=64000 + old 7K task — 001 ended at 0.51, never rotated.
#
# bash 3.2 compatible (macOS default).
set -euo pipefail

DEV_TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SBX="${CC_DEMO_SBX:-/tmp/cc-demo}"
WINDOW_TOKENS="${CC_DEMO_WINDOW:-64000}"
PROJ="demo-app"
DEMO_PROJ="$SBX/project/$PROJ"
STORE="$SBX/home/.claude-continuity"
TMUX_S="cc-demo"
POLL=3
TIMEOUT_S=5400

TASK_PROMPT="Build a small CLI tool in this repo. Work autonomously; do not ask questions; complete all four parts in this session. Write all output (chat text, code, comments, commit message) in English only. Do not run git commit before the final part.
Part 1: create wordcount.py — a Python CLI that takes a file path and prints its line, word and character counts (like wc -lwc). Add test_wordcount.py using only the standard library (unittest, no external deps) with at least 4 tests. Run the tests until they pass.
Part 2: add a --json flag that prints the counts as a JSON object. Add at least 2 tests for it, then run the full suite again.
Part 3: add a --csv flag that prints a header row followed by one data row. Add at least 2 tests for it, then run the full suite again.
Part 4: add a --limit N option that counts only the first N lines (must work with the plain, --json and --csv output). Add at least 2 tests, run the full suite, then git commit everything with a clear commit message."

phase() { printf '\n\033[1;36m=== PHASE: %s ===\033[0m\n' "$1"; }
fail()  { printf '\n\033[1;31mDEMO FAILED: %s\033[0m\n' "$1" >&2; exit 1; }

# --- helpers ---------------------------------------------------------------

sess_state() {  # $1=proj_dir $2=nnn  -> state ("" if missing)
  python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("state", ""))
except Exception:
    pass' "$1/sessions/$2/state.json" 2>/dev/null
}

sess_ratio_pct() {  # $1=proj_dir $2=nnn  -> "NN" or ""
  python3 -c '
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    m = json.load(open(sys.argv[2] + "/runtime/" + s["session_id"] + ".json"))
    print(int(round(m["ratio"] * 100)))
except Exception:
    pass' "$1/sessions/$2/state.json" "$STORE" 2>/dev/null
}

turn_settled() {  # $1=proj_dir $2=nnn  -> 0 when the turn ended recently
  python3 -c '
import json, sys, time
try:
    s = json.load(open(sys.argv[1]))
    ok = (not s.get("turn_active")) and (time.time() - (s.get("last_stop_ts") or 0) < 90)
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)' "$1/sessions/$2/state.json" 2>/dev/null
}

# --- phases -----------------------------------------------------------------

clean() {
  phase "clean"
  tmux kill-session -t "$TMUX_S" 2>/dev/null || true
  rm -rf "$SBX"
  echo "sandbox removed"
}

preflight() {
  command -v tmux    >/dev/null || fail "tmux not on PATH"
  command -v claude  >/dev/null || fail "claude not on PATH"
  command -v python3 >/dev/null || fail "python3 not on PATH"
  [[ -d "$DEV_TREE/cc_continuity" ]] || fail "not the dev tree: $DEV_TREE"
  for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY; do
    eval "val=\${$v:-}"
    [[ -n "$val" ]] || fail "$v not set in this shell (run from your dev shell)"
    case "$val" in *\'*) fail "$v contains a single quote (unsafe to bake into the pane command)";; esac
  done
  echo "model endpoint: ${ANTHROPIC_BASE_URL} (model: ${ANTHROPIC_MODEL:-default})"
  # A local model server can hang silently (known failure mode, no error
  # returned). A tiny generation now fails the run BEFORE the recorder
  # wastes an hour on a frozen model, instead of 17 minutes in.
  if ! curl -sf -m 90 "${ANTHROPIC_BASE_URL}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "x-api-key: ${ANTHROPIC_API_KEY}" \
        -d "{\"model\":\"${ANTHROPIC_MODEL}\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with: go\"}]}" >/dev/null; then
    fail "model endpoint did not answer a tiny request in 90s — the model server may be hung; check it and re-run"
  fi
  echo "model endpoint answered the probe"
}

build_sandbox() {
  phase "sandbox (throwaway HOME + fake project)"
  rm -rf "$SBX"
  mkdir -p "$SBX/home" "$DEMO_PROJ"
  # Pre-seed CC onboarding state: a fresh HOME would otherwise show the
  # welcome/theme picker in the pane instead of the prompt.
  local ver
  ver="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  printf '{"hasCompletedOnboarding": true, "lastOnboardingVersion": "%s", "numStartups": 1}\n' \
    "$ver" > "$SBX/home/.claude.json"
  # Pre-seed folder trust for the demo project (CC resolves /tmp ->
  # /private/tmp, so seed the realpath) — kills the trust dialog entirely.
  python3 - "$SBX/home/.claude.json" "$DEMO_PROJ" <<'EOF'
import json, os, sys
p, proj = sys.argv[1], os.path.realpath(sys.argv[2])
d = json.load(open(p))
d.setdefault("projects", {}).setdefault(proj, {})["hasTrustDialogAccepted"] = True
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("trust pre-seeded for", proj)
EOF
  (
    cd "$DEMO_PROJ"
    git init -q
    git config user.name "Demo User"
    git config user.email "demo@example.com"
    printf '# demo-app\n\nA tiny demo project used by the cc-continuity GIF.\n' > README.md
    git add README.md
    git commit -qm "initial commit"
  )
  echo "sandbox: $SBX"
}

install_sandbox() {
  phase "install (real, into the sandbox HOME)"
  ( cd "$DEV_TREE" && env HOME="$SBX/home" python3 -m cc_continuity install --ntfy off )
  # Demo tuning — invisible to the model (sandbox config only):
  #  - unattended: the pane runs claude with --dangerously-skip-permissions
  #    (the same mode the real unattended dev loop uses)
  #  - window_tokens: a small window so the context bar fills within a few
  #    turns and AUTO rotation fires mid-task (the zero-touch moment)
  python3 - "$STORE/config.json" "$WINDOW_TOKENS" <<'EOF'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["unattended"] = True
cfg["window_tokens"] = int(sys.argv[2])
# Match the user's live install: 1 = the separate animated progress pane
# (the dotted bar + "◆ NN% Active Usage" two-line look). Mode 0 (the
# statusline shimmer) only re-renders on model activity, so an idle or
# slow first turn reads as a dead static line — exactly the look the
# README GIF must not show.
cfg["progress_type"] = 1
with open(p, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"config: unattended=true, window_tokens={cfg['window_tokens']}, progress_type=1")
EOF
  # Pre-seed the bypass-permissions consent (CC writes this to the user
  # settings the first time the "Bypass Permissions mode" dialog is
  # accepted) — keeps the pane from stopping on it.
  python3 - "$SBX/home/.claude/settings.json" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["skipDangerousModePermissionPrompt"] = True
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("settings: skipDangerousModePermissionPrompt=true")
EOF
}

open_pane() {
  phase "pane (real claude -> session $PROJ-001)"
  # Sandbox env for the pane: HOME/PATH point at the sandbox install so
  # cc-continuity resolves to the SANDBOX copy (never the user's real
  # app); USER/LOGNAME/HOSTNAME keep the prompt privacy-clean.
  # The pane command mirrors what `cc-continuity launch` runs in tmux mode
  # (explicit CC_CONTINUITY_NAME + -n + --dangerously-skip-permissions):
  # a pane's shell is non-interactive, so the .zshrc wrapper never fires
  # inside a pane — launch's tmux-mode command IS the real product path.
  local cmd
  # LANG/LC_ALL=C: the pane inherits this shell's locale, which under a
  # non-C locale makes git print localized branch names into the recorded
  # GIF. Python 3 auto-enables UTF-8 mode under the C locale, so model tool
  # runs are unaffected.
  cmd="env HOME=$SBX/home PATH=$SBX/home/.local/bin:\$PATH USER=demo LOGNAME=demo HOSTNAME=demo-box LANG=C LC_ALL=C CC_CONTINUITY_NAME=$PROJ-001 ANTHROPIC_API_KEY='${ANTHROPIC_API_KEY}' ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL}'"
  if [[ -n "${ANTHROPIC_MODEL:-}" ]]; then
    cmd="$cmd ANTHROPIC_MODEL='${ANTHROPIC_MODEL}'"
  fi
  if [[ -n "${ANTHROPIC_SMALL_FAST_MODEL:-}" ]]; then
    cmd="$cmd ANTHROPIC_SMALL_FAST_MODEL='${ANTHROPIC_SMALL_FAST_MODEL}'"
  fi
  cmd="$cmd command claude --dangerously-skip-permissions -n $PROJ-001"
  tmux kill-session -t "$TMUX_S" 2>/dev/null || true
  tmux new-session -d -s "$TMUX_S" -c "$DEMO_PROJ" "$cmd"
  # The progress pane is split LATER by the product (ensure_progress_pane)
  # and does NOT inherit pane 0.0's env prefix — it gets the tmux
  # server's default env, i.e. the user's real HOME. The animator would
  # then read the user's runtime dir while the status line feeds the
  # sandbox's, and the pane freezes on "..." (run-4 bug). A session-scope
  # HOME override reaches exactly the panes created in this session
  # (pane 0.0 already carries the same value from the env prefix).
  tmux set-environment -t "$TMUX_S" HOME "$SBX/home"
  echo "attach: tmux attach -t $TMUX_S   (start recording once the prompt is up)"
}

input_box_ready() {  # $1=pane text -> 0 when the idle input box is visible
  # The live input box is an empty col-0 "❯" line — ❯ + NBSP (U+00A0, NOT
  # a plain space) — with "─" divider lines on both sides (the one above
  # may carry a session-name label). Scrollback user messages also start
  # with ❯ but carry text; picker options are indented.
  local nbsp; nbsp="$(printf '\302\240')"
  printf '%s\n' "$1" | awk -v nbsp="$nbsp" '
    /^[─]/ && prev ~ ("^❯[ \t" nbsp "]*$") && above ~ /^[─]/ { found = 1 }
    { above = prev; prev = $0 }
    END { exit(found ? 0 : 1) }'
}

wait_for_prompt() {
  phase "waiting for the input box"
  local start; start=$(date +%s)
  local picker_handled=0 trust_handled=0 bypass_handled=0
  while :; do
    local now; now=$(date +%s)
    (( now - start > 180 )) && {
      echo "pane is not at the prompt after 180s — current pane:"
      tmux capture-pane -t "$TMUX_S" -p | tail -25
      fail "check the pane (onboarding/trust dialog?) and re-run"
    }
    local t; t="$(tmux capture-pane -t "$TMUX_S" -p 2>/dev/null || true)"
    # First-run dialogs. Separate ifs (not a case): a dismissed dialog's
    # text stays in scrollback and would shadow the other branch, and each
    # handler is one-shot (flag-guarded) so a scrollback re-hit is a no-op.
    if printf '%s' "$t" | grep -q "trust this folder"; then
      if (( trust_handled == 0 )); then
        # Folder-trust dialog — pre-seeded away in build_sandbox; this is
        # the fallback. Default highlight is "1. Yes, I trust this folder".
        case "$t" in
          *"❯ 1. Yes, I trust this folder"*)
            echo "folder-trust dialog — accepting"
            tmux send-keys -t "$TMUX_S" Enter
            trust_handled=1
            ;;
          *)
            echo "trust dialog highlight unexpected — pane now:"
            tmux capture-pane -t "$TMUX_S" -p | tail -20
            fail "check the trust dialog and re-run"
            ;;
        esac
      fi
    fi
    if printf '%s' "$t" | grep -q "Bypass Permissions mode"; then
      if (( bypass_handled == 0 )); then
        # Bypass consent (--dangerously-skip-permissions) — pre-seeded via
        # skipDangerousModePermissionPrompt; this is the fallback. Default
        # highlight is "1. No, exit", so Down first, verify, then Enter.
        echo "bypass consent up — accepting"
        tmux send-keys -t "$TMUX_S" Down
        sleep 1
        local t2; t2="$(tmux capture-pane -t "$TMUX_S" -p 2>/dev/null || true)"
        case "$t2" in
          *"❯ 2. Yes, I accept"*)
            tmux send-keys -t "$TMUX_S" Enter
            bypass_handled=1
            ;;
          *)
            echo "bypass consent highlight unexpected — pane now:"
            tmux capture-pane -t "$TMUX_S" -p | tail -20
            fail "check the bypass consent and re-run"
            ;;
        esac
      fi
    fi
    if printf '%s' "$t" | grep -q "Do you want to use this API key"; then
      if (( picker_handled == 0 )); then
        # CC 2.1.209 first-run picker (the pane env carries
        # ANTHROPIC_API_KEY for the local model). It defaults to "2. No
        # (recommended)", so Up first, verify the highlight moved to "1.
        # Yes", then Enter. It also shows in the GIF — a real local-LLM
        # first launch — trim in post if unwanted.
        echo "API-key picker up — selecting Yes"
        tmux send-keys -t "$TMUX_S" Up
        sleep 1
        local t2; t2="$(tmux capture-pane -t "$TMUX_S" -p 2>/dev/null || true)"
        case "$t2" in
          *"❯ 1. Yes"*)
            tmux send-keys -t "$TMUX_S" Enter
            picker_handled=1
            ;;
          *)
            echo "picker highlight not on Yes — pane now:"
            tmux capture-pane -t "$TMUX_S" -p | tail -20
            fail "check the picker and re-run"
            ;;
        esac
      fi
    fi
    if input_box_ready "$t"; then
      echo "input box up — $(date +%H:%M:%S)"
      return 0
    fi
    sleep 2
  done
}

send_task_gated() {  # CC_DEMO_GATE=1 — the human confirms the recorder before Enter
  phase "sending the task prompt (GATED — human confirms the recorder first)"
  tmux load-buffer -b demotask - <<EOF
$TASK_PROMPT
EOF
  # Paste the task into the idle input box but do NOT submit: the pane sits
  # at "◆ 0% Active Usage" with the prompt visible, the human opens
  # cc-demo and starts the screen recording, and the operator touches
  # $SBX/GO only after the human confirms the recorder is rolling — then
  # Enter fires the task, so the recording never misses the start.
  tmux paste-buffer -b demotask -p -d -t "$TMUX_S"
  echo "task text pasted into the input box — NOT submitted"
  echo "GATED: waiting for $SBX/GO (operator touches it once the human confirms the recording started)"
  local start; start=$(date +%s)
  while [[ ! -f "$SBX/GO" ]]; do
    (( $(date +%s) - start > 1800 )) && fail "gate open 1800s without GO — human did not confirm; re-run"
    sleep 2
  done
  tmux send-keys -t "$TMUX_S" Enter
  echo "GO received — task submitted, watch the pane work"
}

send_task() {
  phase "sending the task prompt"
  # Recorder grace: the input box is up but the task has NOT gone in yet.
  # This is the human's window to attach (tmux attach -t cc-demo) and start
  # the screen recording, so the GIF opens on the idle prompt and shows the
  # task arriving on screen.
  echo "recorder grace: input box up — attach (tmux attach -t $TMUX_S) and start the screen recording; the task goes in after 120s"
  sleep 120
  tmux load-buffer -b demotask - <<EOF
$TASK_PROMPT
EOF
  tmux paste-buffer -b demotask -p -d -t "$TMUX_S"
  tmux send-keys -t "$TMUX_S" Enter
  echo "prompt sent — watch the pane work"
}

monitor() {
  phase "monitoring (auto-rotation expected; the local model is slow — be patient)"
  local proj_dir="" start now
  start=$(date +%s)
  # Registration lands a minute or so after the task is sent (picker accept,
  # first turn, SessionStart hook) — poll for it. (pipefail: a non-matching
  # glob must not kill the script via set -e before the clean fail().)
  while [[ -z "$proj_dir" ]]; do
    now=$(date +%s)
    (( now - start > 300 )) && fail "no session registered in 300s — check the pane: tmux attach -t $TMUX_S"
    proj_dir="$(ls -d "$STORE/projects/"demo-app-* 2>/dev/null | head -1 || true)"
    sleep "$POLL"
  done
  local last="" settle_hits=0
  while :; do
    local now; now=$(date +%s)
    if (( now - start > TIMEOUT_S )); then
      fail "timeout (${TIMEOUT_S}s) — check the pane: tmux attach -t $TMUX_S"
    fi
    # Model-hang guard: the first assistant entry lands only when the
    # model's first thinking block finishes — a small local model can spend
    # 5-10 min in one thinking block (2026-10-22 run: an early 240s guard
    # false-fired at 4m19s into a healthy 21m turn). So: after 900s with
    # zero assistant entries, note it and keep waiting — a hung endpoint is
    # caught by the preflight probe before the recording starts.
    if (( now - start > 900 )); then
      local asst
      asst="$( { grep -hc '"type":"assistant"' "$SBX"/home/.claude/projects/*/*.jsonl 2>/dev/null || true; } | awk '{s+=$1} END {print s+0}')"
      if [[ "$asst" -eq 0 ]]; then
        echo "  [t=$(( (now - start) / 60 ))m] no assistant entry yet — the model is probably still thinking, keep waiting"
      fi
    fi
    local nnn; nnn="$(ls "$proj_dir/sessions" 2>/dev/null | sort | tail -1 || true)"
    if [[ -n "$nnn" ]]; then
      local st sig; st="$(sess_state "$proj_dir" "$nnn")"
      local rp; rp="$(sess_ratio_pct "$proj_dir" "$nnn")"
      sig="$nnn:$st:${rp:-?}"
      # Transition echoes live inside the sig guard: a state that persists
      # across polls (e.g. ROTATION_PENDING through a whole turn) must not
      # re-echo every 3s. sig carries the integer ratio, so each re-echo is
      # at most one per ratio tick.
      if [[ "$sig" != "$last" ]]; then
        echo "  [t=$(( (now - start) / 60 ))m] session $nnn: state=${st:-?} ratio=${rp:-?}%"
        case "$st" in
          ROTATION_PENDING)    echo "  >>> rotation PENDING — the zero-touch moment (statusline shows PENDING->$PROJ-002)";;
          HANDOFF_REQUESTED)   echo "  >>> handoff requested — the model is writing handoff.draft.md";;
          CHECKPOINT_VERIFIED) echo "  >>> checkpoint verified — the rotator will /clear the pane now";;
          ROTATING)            echo "  >>> ROTATING — watch the pane: /clear + new session";;
          ROTATED)             echo "  >>> rotation complete — successor took over";;
        esac
        last="$sig"
      fi
      if grep -q rotation_failed "$proj_dir/sessions/$nnn/ledger.jsonl" 2>/dev/null; then
        fail "ROTATION_FAILED — see $STORE/logs/rotator.log"
      fi
      local commits; commits="$(git -C "$DEMO_PROJ" rev-list --count HEAD 2>/dev/null || echo 0)"
      # Complete = the task's commit landed AND the LATEST session is
      # settled in a quiet ACTIVE state — never mid-rotation: at 64K,
      # 001 ends its turn at ~75% with commits=2, THEN the Stop hook
      # rotates to 002. Declaring complete at 001's turn end would make
      # the recorder stop before the rotation is even shown (2026-10-22
      # run: watcher fired at 40% no-rotation; a 64K run would have
      # fired just before the rotation). state ACTIVE stable for 3
      # consecutive polls (30s) also covers the race where the turn has
      # settled but the Stop hook has not transitioned the state yet.
      if [[ "$commits" -ge 2 && "$st" == "ACTIVE" ]] && turn_settled "$proj_dir" "$nnn"; then
        settle_hits=$((settle_hits + 1))
      else
        settle_hits=0
      fi
      if [[ $settle_hits -ge 3 ]]; then
        phase "DEMO COMPLETE"
        git -C "$DEMO_PROJ" log --oneline
        echo "record a few more seconds, then stop. Next: --scan, then gif_from_video.sh"
        return 0
      fi
    fi
    sleep "$POLL"
  done
}

scan() {
  phase "privacy scan (pane scrollback + sandbox)"
  local txt="/tmp/cc-demo-scan.txt"
  {
    tmux capture-pane -t "$TMUX_S" -p -S - 2>/dev/null || true
    find "$SBX" -type f \( -name '*.md' -o -name '*.py' -o -name '*.jsonl' -o -name '*.json' \) \
      -exec cat {} + 2>/dev/null || true
  } > "$txt"
  local hits=0 pat trclass
  # The first three patterns are placeholders: swap in YOUR own personal
  # tokens (home dir, internal IPs, email domain) before scanning. The
  # Turkish-character class is built from code points so this file never
  # carries the characters it looks for.
  trclass="$(python3 -c 'print("[" + "".join(chr(c) for c in (0x130, 0x131, 0x11e, 0x11f, 0x15e, 0x15f, 0x150, 0xfc, 0xd6, 0xf6, 0xc7, 0xe7)) + "]")')"
  for pat in '<YOUR_HOME_DIR>' '<INTERNAL_IP>' '<EMAIL_DOMAIN>' "$trclass"; do
    if grep -nE "$pat" "$txt" >/dev/null 2>&1; then
      hits=$((hits + 1))
      echo "HIT: $pat"
      grep -nE "$pat" "$txt" | head -5
    fi
  done
  if [[ $hits -eq 0 ]]; then
    echo "clean — no personal-data hits in pane or sandbox"
  else
    echo "$hits pattern(s) hit — review the lines above before publishing"
  fi
  rm -f "$txt"
}

# --- main --------------------------------------------------------------------

case "${1:-run}" in
  --dry)
    preflight || true
    build_sandbox
    install_sandbox
    echo "dry run OK — sandbox at $SBX (rm -rf it when done)"
    ;;
  --scan)
    scan
    ;;
  --clean)
    clean
    ;;
  run)
    preflight
    clean
    build_sandbox
    install_sandbox
    open_pane
    wait_for_prompt
    if [[ -n "${CC_DEMO_GATE:-}" ]]; then
      send_task_gated
    else
      send_task
    fi
    monitor
    ;;
  *)
    echo "usage: $0 [run|--dry|--scan|--clean]" >&2
    exit 1
    ;;
esac
