#!/usr/bin/env bash
# Take real screenshots of the sorto TUI on a virtual X display. Runs inside the demo VM.
#
#   capture.sh [SCENARIO ...]        SCENARIO: inbox (default), reorg, doctor
#
# inbox   sorto run ~/demo/Inbox -t ~/demo/Archive --once --confirm
# reorg   sorto run ~/demo/Messy -t ~/demo/Messy --once --confirm   (reorganize in place)
# doctor  sorto doctor ~/demo/Inbox -t ~/demo/Archive
#
# sorto runs inside tmux inside xterm on Xvfb. tmux lets the script read the screen
# as text (to know when a file's analysis is on screen) and press keys; xterm gives
# the real font rendering that ImageMagick's `import` captures. With --confirm sorto
# holds each finished analysis until Enter, so every frame is stable.
#
# Env: OUT (default ~/shots), COLS/ROWS (140x60), FONT_SIZE (11), NO_RESET=1 keeps
# the current demo data, FILE_TIMEOUT seconds per file (900), DISPLAY_NUM (99),
# SORTO_ARGS extra run options (e.g. "--llm-model qwen3.5:9b-16k"),
# SWITCH_MODEL_AT=N presses the TUI's m key (switch model) after the Nth confirmation.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HOME/shots}"
COLS="${COLS:-140}"
ROWS="${ROWS:-60}"
FONT_SIZE="${FONT_SIZE:-11}"
FILE_TIMEOUT="${FILE_TIMEOUT:-900}"
DNUM="${DISPLAY_NUM:-99}"
export DISPLAY=":$DNUM"
SORTO="${SORTO:-$(command -v sorto || echo "$HOME/.local/bin/sorto")}"
SESSION=sortodemo
TMUXCONF="$OUT/.tmux.conf"
read -r -a EXTRA <<<"${SORTO_ARGS:-}"
SWITCH_MODEL_AT="${SWITCH_MODEL_AT:-0}"
BG='#0f1419'

mkdir -p "$OUT"
log() { printf '[capture %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

cat >"$TMUXCONF" <<'EOF'
set -g status off
set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
set -sg escape-time 0
set -g remain-on-exit off
EOF

start_x() {
  if ! xdpyinfo >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1920x1200x24 -nolisten tcp >"$OUT/.xvfb.log" 2>&1 &
    for _ in $(seq 1 50); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.2; done
  fi
  xsetroot -solid "$BG" 2>/dev/null || true
}

# number of "plan" events in sorto's log = analyses finished so far (screen-size independent)
plans() { cat "${XDG_STATE_HOME:-$HOME/.local/state}"/sorto/*/sorto.log 2>/dev/null | grep -c ' plan ' || true; }

pane() { tmux -f "$TMUXCONF" capture-pane -p -t "$SESSION" 2>/dev/null || true; }
# the header + NOW panel only (everything above LAST FILED)
now_panel() { pane | awk '/LAST FILED/{exit} {print}'; }
alive() { tmux -f "$TMUXCONF" has-session -t "$SESSION" 2>/dev/null; }

# launch NAME COMMAND...  -- xterm running tmux running COMMAND; sets XWIN
launch() {
  local name="$1"; shift
  tmux -f "$TMUXCONF" kill-session -t "$SESSION" 2>/dev/null || true
  local cmd; cmd="$(printf '%q ' "$@")"
  COLORTERM=truecolor TERM=xterm-256color xterm \
    -name sortodemo -title "sorto" -geometry "${COLS}x${ROWS}+0+0" \
    -fa 'DejaVu Sans Mono' -fs "$FONT_SIZE" -bg "$BG" -fg '#e6edf3' -bw 0 -b 12 +sb \
    -xrm 'XTerm*cursorColor: #0f1419' -xrm 'XTerm*allowBoldFonts: true' \
    -e tmux -f "$TMUXCONF" new-session -s "$SESSION" \
       "env COLORTERM=truecolor $cmd; echo '[sorto exited]'; sleep 4" &
  XWIN=""
  for _ in $(seq 1 100); do
    XWIN="$(xdotool search --classname sortodemo 2>/dev/null | head -1 || true)"
    [ -n "$XWIN" ] && alive && break
    sleep 0.2
  done
  [ -n "$XWIN" ] || { log "xterm did not start"; exit 1; }
  log "$name: xterm window $XWIN"
}

# shot FILE  -- screenshot the xterm window, stripped and palette-reduced
shot() {
  local f="$1"
  import -display "$DISPLAY" -window "$XWIN" "$f.tmp.png"
  if command -v pngquant >/dev/null 2>&1; then
    pngquant --force --quality 80-98 --strip --output "$f" "$f.tmp.png" && rm -f "$f.tmp.png"
  else
    convert "$f.tmp.png" -strip "$f" && rm -f "$f.tmp.png"
  fi
  pane >"${f%.png}.txt"
  log "saved $f ($(du -k "$f" | cut -f1) KB)"
}

slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed 's/__*/_/g; s/^_//; s/_$//' | cut -c1-40; }

# drive a --confirm run: screenshot each analysis awaiting Enter, then accept it
drive_confirm() {
  local prefix="$1" n=0 r=0 t0 fname text seen p rfile last_result=""
  seen="$(plans)"
  while alive; do
    t0=$SECONDS
    # wait for the prompt, or a new plan in the log (prompt may be scrolled off), or exit
    while alive; do
      p="$(now_panel)"
      grep -q 'Move it there?' <<<"$p" && break
      [ "$(plans)" -gt "$seen" ] && break
      # files that need no confirmation (kept / already in the right place): grab their result frame
      if grep -q 'Result:' <<<"$p"; then
        rfile="$(grep -o 'File: *[^│]*' <<<"$p" | head -1 | sed 's/^File: *//; s/ *$//')"
        if [ -n "$rfile" ] && [ "$rfile" != "$last_result" ]; then
          last_result="$rfile"; r=$((r + 1))
          shot "$OUT/$(printf '%s-r%02d-%s' "$prefix" "$r" "$(slug "$rfile")").png"
        fi
      fi
      if (( SECONDS - t0 > FILE_TIMEOUT )); then log "timeout waiting for analysis"; return 1; fi
      sleep 0.7
    done
    alive || break
    seen="$(plans)"
    sleep 1.5   # let the TUI finish its render tick
    text="$(now_panel)"
    fname="$(printf '%s\n' "$text" | grep -o 'File: *[^│]*' | head -1 | sed 's/^File: *//; s/ *$//')"
    n=$((n + 1))
    shot "$OUT/$(printf '%s-%02d-%s' "$prefix" "$n" "$(slug "$fname")").png"
    tmux -f "$TMUXCONF" send-keys -t "$SESSION" Enter
    last_result="$fname"
    if [ "$n" -eq "$SWITCH_MODEL_AT" ]; then
      sleep 0.5; tmux -f "$TMUXCONF" send-keys -t "$SESSION" m; log "pressed m (switch model)"
    fi
    # wait until the prompt is gone before looking for the next one
    for _ in $(seq 1 60); do now_panel | grep -q 'Move it there?' || break; sleep 0.5; done
    sleep 1
    alive && now_panel | grep -q 'Result:' && shot "$OUT/$(printf '%s-%02d-%s-moved' "$prefix" "$n" "$(slug "$fname")").png" || true
  done
  log "$prefix: $n analyses captured"
}

scenario_inbox() {
  launch inbox "$SORTO" run "$HOME/demo/Inbox" -t "$HOME/demo/Archive" --once --confirm "${EXTRA[@]}"
  drive_confirm inbox
}

scenario_reorg() {
  launch reorg "$SORTO" run "$HOME/demo/Messy" -t "$HOME/demo/Messy" --once --confirm "${EXTRA[@]}"
  drive_confirm reorg
}

scenario_doctor() {
  launch doctor bash -c "clear; tput civis; $(printf '%q' "$SORTO") doctor ~/demo/Inbox -t ~/demo/Archive; sleep 30"
  for _ in $(seq 1 120); do pane | grep -q 'checks passed' && break; sleep 1; done
  sleep 1
  shot "$OUT/doctor.png"
  # crop the empty terminal area below the report
  convert "$OUT/doctor.png" -trim +repage -bordercolor "$BG" -border 16 "$OUT/doctor.png"
  tmux -f "$TMUXCONF" kill-session -t "$SESSION" 2>/dev/null || true
}

main() {
  local scenarios=("$@")
  [ ${#scenarios[@]} -gt 0 ] || scenarios=(inbox)
  if [ -z "${NO_RESET:-}" ]; then bash "$HERE/make-demo.sh" --reset; else bash "$HERE/make-demo.sh"; fi
  curl -fsS -m 10 http://127.0.0.1:11434/v1/models >/dev/null \
    || { log "local model endpoint 127.0.0.1:11434 not reachable (is the ssh -R tunnel up?)"; exit 1; }
  start_x
  for s in "${scenarios[@]}"; do
    log "scenario $s"
    "scenario_$s"
    sleep 5
  done
  tmux -f "$TMUXCONF" kill-server 2>/dev/null || true
  log "done; screenshots in $OUT"
}

main "$@"
