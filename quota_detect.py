"""Quota / usage-limit detection for wrapped agent CLIs.

When an agent burns through its provider quota (subscription usage window,
rate limit, credit balance), the CLI just stops responding — and the agent
cannot announce it, because sending a chat message via MCP is itself a
model turn, which is exactly what the exhausted quota blocks. The result
is a silent death: humans and other agents keep @mentioning an agent that
will never answer.

The wrapper solves this out-of-band. It already polls the agent's visible
terminal (tmux capture-pane on Mac/Linux, console screen buffer on
Windows) for login prompts; QuotaLimitDetector runs over the same
snapshots looking for quota/rate-limit exhaustion messages. Detection
costs zero agent quota. On confirmation the wrapper writes a
``{agent}_quota_exhausted`` flag file; the server's background checker
posts a system message on the agent's behalf, so humans see it in the UI
and other agents see it the next time they read the channel. A
``{agent}_quota_resolved`` flag posts the all-clear once the limit
message leaves the screen.

Reset tracking: most CLIs print when the window reopens — Claude Code
shows "resets 3am" (rolling 5-hour window) or "resets Jul 24" (weekly),
Codex says "Try again in 4 hours 32 minutes", etc. parse_reset_time()
extracts that into an absolute timestamp which rides along in the flag
payload; the server announces it, warns anyone who @mentions the dead
agent, and posts a "should be back" notice once the time passes. When
the CLI doesn't print a time, a per-agent ``quota_reset_hours`` config
value (e.g. 5 for Claude's rolling window) provides an estimate. For
exact figures a human can always run ``/usage`` in the agent's terminal.
"""

import json
import re
import time
from pathlib import Path

from login_detect import CLEAR_POLLS, CONFIRM_POLLS, ScreenPatternDetector

# Phrases that indicate the CLI hit a quota / usage / rate limit and can no
# longer make model calls. Matched case-insensitively against the visible
# terminal text. Covers the built-in providers (Claude Code, Codex, Gemini,
# Copilot, Qwen, Kimi, ...) plus generic wording. Per-agent extras can be
# added via `quota_patterns` in config.toml.
#
# NOTE: the system messages the server posts about quota exhaustion must
# NOT match these patterns — chat text can appear in other agents'
# terminals when they read the channel, and a match there would raise a
# false alarm for the wrong agent.
DEFAULT_QUOTA_PATTERNS = [
    # Generic
    r"(?:usage|rate|spend(?:ing)?|token|request|message|session|weekly|daily|monthly|\d+[ -]?hour)[ -]?limits? (?:reached|exceeded|hit)",
    r"you(?:'ve| have) (?:reached|hit|exceeded) (?:your|the)(?: [\w'-]+){0,3} (?:limit|quota)s?",
    r"quota (?:limit )?(?:has been |was )?(?:exceeded|exhausted|reached)",
    r"out of (?:free )?(?:quota|credits?|tokens)",
    r"no (?:remaining|more) (?:quota|credits?|tokens)",
    r"insufficient (?:quota|credits?|balance|funds)",
    # Anthropic / Claude Code
    r"out of extra usage",
    r"credit balance is too low",
    # OpenAI / Codex
    r"too many requests",
    r"(?:status(?: code)?|error|http)\D{0,3}429\b",
    # Google / Gemini
    r"resource[_ ]exhausted",
]


# Marker carried by every quota system message the server posts. Chat text
# gets echoed into other agents' terminals when they read the channel; lines
# carrying this marker are excluded from matching so an announcement about
# agent A can't raise a false alarm for agent B.
ECHO_MARKER = "\U0001faab"  # 🪫


def sanitize_for_chat(text: str) -> str:
    """Make a detected detail line safe to quote in chat.

    Replaces spaces with no-break spaces so the echoed text can't re-trigger
    detection when it scrolls through another agent's terminal (the patterns
    match literal spaces). Renders identically everywhere."""
    return (text or "").replace(" ", " ")


class QuotaLimitDetector(ScreenPatternDetector):
    """Detects quota/usage-limit exhaustion in agent terminals.

    poll(screen_text) returns:
      ("quota_exhausted", detail)  — limit confirmed; notify the room once
      ("resolved", "")             — limit message has cleared
      None                         — no state change
    """

    def __init__(self, extra_patterns=None, confirm_polls: int = CONFIRM_POLLS,
                 clear_polls: int = CLEAR_POLLS):
        super().__init__(
            list(DEFAULT_QUOTA_PATTERNS) + list(extra_patterns or []),
            "quota_exhausted", confirm_polls, clear_polls,
        )

    def poll(self, screen_text):
        if screen_text and ECHO_MARKER in screen_text:
            screen_text = "\n".join(
                line for line in screen_text.splitlines()
                if ECHO_MARKER not in line
            )
        return super().poll(screen_text)


# ---------------------------------------------------------------------------
# Reset-time parsing
# ---------------------------------------------------------------------------

# "Try again in 4 hours 32 minutes" / "retry in 2h 5m" / "available again in 30 minutes"
_RELATIVE_RE = re.compile(
    r"(?:try again|retry|resets?|available(?: again)?|back)\s+in\s+(?:about\s+|~\s*)?"
    r"((?:\d+\s*(?:days?|d\b|hours?|hrs?|h\b|minutes?|mins?|m\b|seconds?|secs?|s\b)[,\s]*)+)",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(
    r"(\d+)\s*(days?|d\b|hours?|hrs?|h\b|minutes?|mins?|m\b|seconds?|secs?|s\b)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

# "resets Jul 24" / "resets on July 24 at 11:59pm"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE_RE = re.compile(
    r"resets?\s+(?:on\s+)?(" + _MONTHS + r")[a-z]*\s+(\d{1,2})"
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)

# "resets 3am" / "resets at 4:30pm" / "resets at 14:30"
_CLOCK_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE,
)


def _clock_to_epoch(hour: int, minute: int, ampm: str, base) -> float | None:
    """Epoch for the next occurrence of hour:minute after base (struct_time)."""
    if ampm:
        if hour < 1 or hour > 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    elif hour > 23 or minute is None:
        return None  # bare "resets 3" is too ambiguous without am/pm or :MM
    candidate = time.mktime((base.tm_year, base.tm_mon, base.tm_mday,
                             hour, minute or 0, 0, -1, -1, -1))
    if candidate <= time.mktime(base):
        candidate += 86400  # already passed today — roll to tomorrow
    return candidate


def parse_reset_time(text: str, now: float | None = None) -> float | None:
    """Extract a quota-reset timestamp (epoch seconds, local time) from
    terminal text, or None if no reset time is stated.

    Handles the common CLI phrasings:
      - relative:  "Try again in 4 hours 32 minutes", "retry in 2h"
      - clock:     "resets 3am", "resets at 4:30pm", "resets at 14:30"
      - date:      "resets Jul 24", "resets on Jul 24 at 11:59pm"

    Timezone annotations like "(Asia/Phnom_Penh)" are ignored — times are
    interpreted in the server's local timezone, which matches what the CLI
    shows on the same machine.
    """
    if not text:
        return None
    now = time.time() if now is None else now
    base = time.localtime(now)

    m = _RELATIVE_RE.search(text)
    if m:
        total = 0
        for qty, unit in _UNIT_RE.findall(m.group(1)):
            total += int(qty) * _UNIT_SECONDS[unit[0].lower()]
        if total > 0:
            return now + total

    m = _DATE_RE.search(text)
    if m:
        month = ("jan", "feb", "mar", "apr", "may", "jun",
                 "jul", "aug", "sep", "oct", "nov", "dec").index(m.group(1).lower()) + 1
        day = int(m.group(2))
        hour, minute = 0, 0
        if m.group(3):
            hour = int(m.group(3))
            minute = int(m.group(4) or 0)
            if m.group(5):
                hour = hour % 12 + (12 if m.group(5).lower() == "pm" else 0)
        try:
            candidate = time.mktime((base.tm_year, month, day, hour, minute, 0, -1, -1, -1))
            if candidate <= now:
                candidate = time.mktime((base.tm_year + 1, month, day, hour, minute, 0, -1, -1, -1))
            return candidate
        except (ValueError, OverflowError):
            return None

    m = _CLOCK_RE.search(text)
    if m:
        return _clock_to_epoch(int(m.group(1)),
                               int(m.group(2)) if m.group(2) else None,
                               m.group(3) or "", base)

    return None


# ---------------------------------------------------------------------------
# Flag files — same channel as the login flags: the wrapper writes them into
# data_dir, the server's background checker posts a system message and
# deletes them.
# ---------------------------------------------------------------------------

def write_quota_exhausted_flag(data_dir: Path, agent_name: str, detail: str = "",
                               attach_hint: str = "",
                               reset_at: float | None = None,
                               reset_estimated: bool = False) -> None:
    try:
        flag = Path(data_dir) / f"{agent_name}_quota_exhausted"
        payload = {"agent": agent_name, "detail": detail,
                   "attach_hint": attach_hint, "reset_at": reset_at,
                   "reset_estimated": reset_estimated}
        flag.write_text(json.dumps(payload), "utf-8")
    except Exception:
        pass


def write_quota_resolved_flag(data_dir: Path, agent_name: str) -> None:
    try:
        flag = Path(data_dir) / f"{agent_name}_quota_resolved"
        flag.write_text(agent_name, "utf-8")
    except Exception:
        pass
