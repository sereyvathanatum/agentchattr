"""Login/auth prompt detection for wrapped agent CLIs.

Agent CLIs sometimes stop and wait for a human — an OAuth session expired,
or the CLI was launched for the first time and wants an interactive login.
The agent can't respond to chat mentions while stuck on that screen, and
nothing in the chat room explains why.

The wrapper polls the agent's visible terminal output (tmux capture-pane on
Mac/Linux, console screen buffer on Windows) and runs it through
LoginPromptDetector. When a login prompt is confirmed, the wrapper writes a
``{agent}_login_required`` flag file; the server's background checker picks
it up and posts a system message in the chat UI so the owner can attach to
the terminal and complete the login. When the prompt clears, a
``{agent}_login_resolved`` flag posts the all-clear.
"""

import json
import re
from pathlib import Path

# Phrases that indicate the CLI is waiting for interactive authentication.
# Matched case-insensitively against the visible terminal text. Covers the
# built-in providers (Claude Code, Codex, Gemini, Copilot, Qwen, Kimi,
# CodeBuddy, ...) plus generic wording. Per-agent extras can be added via
# `login_patterns` in config.toml.
DEFAULT_LOGIN_PATTERNS = [
    # Generic
    r"select (?:login|auth(?:entication)?) method",
    r"please (?:log ?in|sign ?in|authenticate)",
    r"(?:log ?in|sign ?in|authentication) (?:is )?required",
    r"not (?:currently )?logged in",
    r"session (?:has )?(?:expired|timed out)",
    r"(?:auth(?:entication)?|oauth|access) token (?:has )?(?:expired|been revoked)",
    r"credentials? (?:have |has )?expired",
    r"authentication (?:failed|error|expired)",
    # Claude Code
    r"run /login",
    r"invalid api key",
    # Codex / Copilot / device-code flows
    r"sign in with",
    r"(?:one-time|device) code",
    r"paste (?:the )?code",
    # Gemini / Qwen
    r"waiting for auth",
]

# How many consecutive matching polls before we report login_required
# (filters one-poll blips like scrolling chat text), and how many
# consecutive clean polls before we report resolved (filters redraw
# flicker while the user is mid-login).
CONFIRM_POLLS = 2
CLEAR_POLLS = 5

_DETAIL_MAX_LEN = 120


class LoginPromptDetector:
    """State machine over successive terminal snapshots.

    poll(screen_text) returns:
      ("login_required", detail)  — prompt confirmed; notify the owner once
      ("resolved", "")            — previously reported prompt has cleared
      None                        — no state change
    """

    def __init__(self, extra_patterns=None, confirm_polls: int = CONFIRM_POLLS,
                 clear_polls: int = CLEAR_POLLS):
        patterns = list(DEFAULT_LOGIN_PATTERNS) + list(extra_patterns or [])
        self._patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._confirm_polls = max(1, confirm_polls)
        self._clear_polls = max(1, clear_polls)
        self._notified = False
        self._hits = 0
        self._misses = 0

    def _match(self, screen_text: str) -> str:
        for pattern in self._patterns:
            m = pattern.search(screen_text)
            if m:
                # Return the surrounding line, trimmed, for context in the UI
                start = screen_text.rfind("\n", 0, m.start()) + 1
                end = screen_text.find("\n", m.end())
                if end == -1:
                    end = len(screen_text)
                line = screen_text[start:end].strip()
                return line[:_DETAIL_MAX_LEN] or m.group(0)
        return ""

    def poll(self, screen_text: str):
        detail = self._match(screen_text or "")
        if not self._notified:
            if detail:
                self._hits += 1
                if self._hits >= self._confirm_polls:
                    self._notified = True
                    self._misses = 0
                    return ("login_required", detail)
            else:
                self._hits = 0
        else:
            if detail:
                self._misses = 0
            else:
                self._misses += 1
                if self._misses >= self._clear_polls:
                    self._notified = False
                    self._hits = 0
                    return ("resolved", "")
        return None


# ---------------------------------------------------------------------------
# Flag files — same channel as the *_recovered recovery flags: the wrapper
# writes them into data_dir, the server's background checker posts a system
# message and deletes them.
# ---------------------------------------------------------------------------

def write_login_required_flag(data_dir: Path, agent_name: str, detail: str = "",
                              attach_hint: str = "") -> None:
    try:
        flag = Path(data_dir) / f"{agent_name}_login_required"
        payload = {"agent": agent_name, "detail": detail, "attach_hint": attach_hint}
        flag.write_text(json.dumps(payload), "utf-8")
    except Exception:
        pass


def write_login_resolved_flag(data_dir: Path, agent_name: str) -> None:
    try:
        flag = Path(data_dir) / f"{agent_name}_login_resolved"
        flag.write_text(agent_name, "utf-8")
    except Exception:
        pass
