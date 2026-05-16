#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Claude Session: Newest
# @raycast.mode silent
# @raycast.packageName Claude Sessions
# @raycast.icon ⚡
# @raycast.description Jump to the newest (lowest-age) live Claude Code session.

export PATH="/opt/homebrew/bin:/Users/jerryluo/.local/bin:$PATH"
export TERM_PROGRAM=WezTerm
exec uv run --script /Users/jerryluo/scripts/claude-session-jump.py newest
