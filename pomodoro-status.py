#!/usr/bin/env python3
"""Pomodoro timer -> blackhole.glsl bridge: grow the hole as you work, shrink
when idle, via the cursor color channel (MODE_TOKENS).

Wired two ways (both optional, use either or both):

  1. Claude Code statusLine/hooks — runs on every assistant turn:
       ~/.claude/settings.json:
         { "statusLine": { "type": "command",
             "command": "/path/to/pomodoro-status.py" },
           "hooks": {
             "SessionStart": [ ... ],
             "SessionEnd":   [ ... ] } }

  2. Shell precmd — runs before every prompt:
       precmd() { /path/to/pomodoro-status.py; }

The hole grows at a constant rate (0→1 over WORK_MIN of *active* wall-clock
time) and shrinks at a faster constant rate (1→0 over SHRINK_MIN).  'Active'
means the script received a call (from Claude's statusLine, a shell prompt,
or any trigger) within the last IDLE_TIMEOUT seconds.  When active the hole
grows; when idle it shrinks — no fixed work/break cycles, just rates.

Requires `SIZE_MODE MODE_TOKENS` in blackhole.glsl (the default).
"""

import json
import os
import sys
import time

# ------------------------------------------------------------------- config --
WORK_MIN    = 55     # minutes of cumulative activity to grow 0 → 1
SHRINK_MIN  = 5      # minutes of silence to shrink 1 → 0
IDLE_TIMEOUT = 90    # seconds without a call before switching to shrink

STATE_DIR  = os.path.expanduser("~/.local/state")
STATE_FILE = os.path.join(STATE_DIR, "ghostty-pomodoro.json")

# Must match claude-token.py's CURSOR_BASE and the shader's TOKEN_BASE_HI.
CURSOR_BASE = (0xF0, 0xB0, 0x00)

# Constant rates (per second)
GROWTH_RATE = 1.0 / (WORK_MIN * 60.0)     # 0.000303… / s
SHRINK_RATE = 1.0 / (SHRINK_MIN * 60.0)   # 0.003333… / s


# --------------------------------------------------------- OSC 12 : encode  -- (from claude-token.py)
def emit(seq):
    """Write an escape sequence to this session's terminal (/dev/tty)."""
    try:
        with open("/dev/tty", "wb") as tty:
            tty.write(seq)
        return
    except OSError:
        pass


def apply(level):
    """Encode level (0..1) into cursor color via OSC 12, or clear with OSC 112."""
    if level < 0.0:
        emit(b"\033]112\007")
        return
    fill = max(0, min(250, int(round(level * 250.0))))
    hi, lo = fill >> 4, fill & 0xF
    rgb = (CURSOR_BASE[0] | (hi ^ lo ^ 0x5),
           CURSOR_BASE[1] | hi,
           CURSOR_BASE[2] | lo)
    emit(b"\033]12;#%02x%02x%02x\007" % rgb)


# ------------------------------------------------------- state persistence --
def load_state():
    """Read state file, or return defaults."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    """Write state file atomically."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


# -------------------------------------------------------------- status bar --
def bar(level, width=10):
    filled = max(0, min(width, int(round(level * width))))
    return "█" * filled + "░" * (width - filled)


def read_stdin():
    """Read JSON from stdin if available, or return empty dict."""
    # When called from shell precmd stdin is often a terminal (no data).
    if not sys.stdin.isatty():
        try:
            return json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ------------------------------------------------------------------- main --
def main():
    data = read_stdin()
    event = data.get("hook_event_name")

    # ── Claude SessionEnd ──
    if event == "SessionEnd":
        # Reset cursor to theme default -> no signal -> shader hides the hole
        apply(-1.0)
        return

    # ── Claude SessionStart ──
    if event == "SessionStart":
        state = load_state()
        state["level"] = 0.0
        state["last_check_at"] = time.time()
        state["last_active_at"] = state["last_check_at"]
        save_state(state)
        apply(0.0)
        return

    # ── statusLine / precmd: tick the timer ──
    now = time.time()
    state = load_state()

    # Init first run
    if "level" not in state:
        state["level"] = 0.0
        state["last_check_at"] = now
        state["last_active_at"] = now

    last_check = state.get("last_check_at", now)
    last_active = state.get("last_active_at", now)
    dt = max(0.0, now - last_check)

    # The call itself IS the activity signal (Claude statusLine or shell prompt)
    state["last_active_at"] = now

    # Idle: how long since the PREVIOUS call (before this one)
    idle = now - last_active
    is_active = idle < IDLE_TIMEOUT

    # Apply constant rates
    if is_active:
        state["level"] = min(1.0, state["level"] + dt * GROWTH_RATE)
    else:
        state["level"] = max(0.0, state["level"] - dt * SHRINK_RATE)

    # Write to shader via cursor color
    level = round(state["level"] * 100.0) / 100.0
    apply(level)

    # Persist
    state["last_check_at"] = now
    save_state(state)

    # Print a status line when called as Claude Code statusLine
    # (stdin JSON with no hook_event_name = statusLine call)
    if event is None and data:
        pct = f"{level * 100:.0f}%"
        print(f"\033[2m⚫️ {bar(level)}  {pct}\033[0m")


if __name__ == "__main__":
    main()
