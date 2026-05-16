#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Claude Session: Newer
# @raycast.mode silent
# @raycast.packageName Claude Sessions
# @raycast.icon 🟢
# @raycast.description Jump to a newer live Claude Code session (cycles toward lowest age).

export PATH="/opt/homebrew/bin:/Users/jerryluo/.local/bin:$PATH"
export TERM_PROGRAM=WezTerm
exec uv run --script /Users/jerryluo/scripts/claude-session-jump.py prev
