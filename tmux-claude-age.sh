#!/bin/bash
# Print " │ <age>" if the given pane tty hosts a live Claude Code session,
# otherwise print nothing. Intended for tmux pane-border-format:
#
#   set -g pane-border-format "... #(tmux-claude-age.sh #{pane_tty})"

tty="${1#/dev/}"
[[ -z "$tty" ]] && exit 0

sessions_dir="$HOME/.claude/sessions"
[[ -d "$sessions_dir" ]] || exit 0

shopt -s nullglob
for f in "$sessions_dir"/*.json; do
  pid=$(grep -o '"pid":[[:space:]]*[0-9]*' "$f" | head -1 | grep -o '[0-9]*')
  [[ -n "$pid" ]] || continue
  proc_tty=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
  [[ "$proc_tty" == "$tty" ]] || continue

  ref=$(grep -o '"updatedAt":[[:space:]]*[0-9]*' "$f" | head -1 | grep -o '[0-9]*')
  [[ -n "$ref" ]] || ref=$(grep -o '"startedAt":[[:space:]]*[0-9]*' "$f" | head -1 | grep -o '[0-9]*')
  [[ -n "$ref" ]] || exit 0

  now=$(date +%s)
  age=$(( now - ref / 1000 ))
  (( age < 0 )) && age=0

  if   (( age < 60 ));    then label="${age}s"
  elif (( age < 3600 ));  then label="$(( age / 60 ))m"
  elif (( age < 86400 )); then label="$(( age / 3600 ))h"
  else                         label="$(( age / 86400 ))d"
  fi

  printf '│ %s ' "$label"
  exit 0
done
