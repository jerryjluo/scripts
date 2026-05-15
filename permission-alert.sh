#!/bin/bash

# Get the terminal app (Terminal or iTerm2)
TERM_APP="${TERM_PROGRAM:-Terminal}"

if command -v terminal-notifier &> /dev/null; then
    terminal-notifier \
        -title "Claude Code" \
        -message "Claude Code needs your permission to do something" \
        -activate "com.apple.${TERM_APP}" \
        -sound default
else
    echo "terminal-notifier not found. Install with: brew install terminal-notifier"
    exit 1
fi
