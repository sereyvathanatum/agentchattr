import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quota_detect import (
    QuotaLimitDetector,
    parse_reset_time,
    sanitize_for_chat,
    write_quota_exhausted_flag,
    write_quota_resolved_flag,
)

CLAUDE_SESSION_LIMIT = """\
> continue the refactor

  5-hour limit reached ∙ resets 3am
"""

CLAUDE_WEEKLY_LIMIT = "Claude usage limit reached. Your limit will reset at 4pm (Asia/Phnom_Penh)."

CODEX_LIMIT = """\
■ You've hit your usage limit. Try again in 4 hours 32 minutes.
"""

GEMINI_QUOTA = """\
ApiError: 429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric
'Gemini 2.5 Pro Requests' of service 'generativelanguage.googleapis.com'.
"""

CREDITS_LOW = "Your credit balance is too low to access the Anthropic API."

NORMAL_SCREEN = """\
> analyzing router.py

I found the bug in parse_mentions — fixing it now.
"""

# What the server posts to chat about an exhausted agent. This text can be
# echoed into OTHER agents' terminals when they read the channel, so it must
# never re-trigger the detector (which would raise a false alarm for the
# wrong agent).
SERVER_ANNOUNCEMENT = (
    "🪫 @claude is out of AI provider capacity and can't respond. "
    "Expected back around 15:00. Mentions will be queued for its return; "
    "don't wait on it for time-sensitive work."
)


class QuotaLimitDetectorTests(unittest.TestCase):
    def test_detects_claude_session_limit_after_confirm_polls(self):
        det = QuotaLimitDetector(confirm_polls=2)
        self.assertIsNone(det.poll(CLAUDE_SESSION_LIMIT))
        event = det.poll(CLAUDE_SESSION_LIMIT)
        self.assertIsNotNone(event)
        kind, detail = event
        self.assertEqual(kind, "quota_exhausted")
        self.assertIn("limit reached", detail)
        self.assertIn("resets 3am", detail)

    def test_detects_provider_limit_screens(self):
        for screen in (CLAUDE_WEEKLY_LIMIT, CODEX_LIMIT, GEMINI_QUOTA, CREDITS_LOW):
            det = QuotaLimitDetector(confirm_polls=1)
            event = det.poll(screen)
            self.assertIsNotNone(event, screen)
            self.assertEqual(event[0], "quota_exhausted")

    def test_normal_output_never_triggers(self):
        det = QuotaLimitDetector(confirm_polls=1)
        for _ in range(5):
            self.assertIsNone(det.poll(NORMAL_SCREEN))

    def test_server_announcement_does_not_retrigger(self):
        det = QuotaLimitDetector(confirm_polls=1)
        self.assertIsNone(det.poll(SERVER_ANNOUNCEMENT))

    def test_echoed_announcement_with_detail_does_not_retrigger(self):
        # The full server announcement quotes the original limit line. When
        # another agent reads the channel, that text lands on ITS terminal —
        # the 🪫 marker line-filter must keep its watcher quiet.
        echoed = (
            f'[13:05] system: 🪫 @claude is out of AI provider capacity and '
            f'can\'t respond — its terminal shows: "5-hour limit reached ∙ '
            f'resets 3am". Expected back around 03:00.'
        )
        det = QuotaLimitDetector(confirm_polls=1)
        for _ in range(3):
            self.assertIsNone(det.poll(echoed))

    def test_sanitized_detail_does_not_match_patterns(self):
        # Even without the marker on the same line (terminal wrapping can
        # split the message), the sanitized detail itself must not match.
        detail = sanitize_for_chat("5-hour limit reached ∙ resets 3am")
        det = QuotaLimitDetector(confirm_polls=1)
        self.assertIsNone(det.poll(f'"{detail}". Expected back around 03:00.'))

    def test_sanitize_preserves_visible_text(self):
        self.assertEqual(sanitize_for_chat("limit reached").replace(" ", " "),
                         "limit reached")
        self.assertEqual(sanitize_for_chat(""), "")

    def test_single_poll_blip_does_not_trigger(self):
        det = QuotaLimitDetector(confirm_polls=2)
        self.assertIsNone(det.poll(CODEX_LIMIT))
        self.assertIsNone(det.poll(NORMAL_SCREEN))  # resets streak
        self.assertIsNone(det.poll(CODEX_LIMIT))

    def test_resolved_after_clear_polls_then_can_renotify(self):
        det = QuotaLimitDetector(confirm_polls=1, clear_polls=3)
        self.assertEqual(det.poll(CLAUDE_SESSION_LIMIT)[0], "quota_exhausted")
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        self.assertEqual(det.poll(NORMAL_SCREEN), ("resolved", ""))
        # A later exhaustion notifies again
        self.assertEqual(det.poll(CODEX_LIMIT)[0], "quota_exhausted")

    def test_extra_patterns_from_config(self):
        det = QuotaLimitDetector(extra_patterns=[r"daily allowance depleted"],
                                 confirm_polls=1)
        event = det.poll("Sorry: Daily Allowance Depleted, come back tomorrow")
        self.assertEqual(event[0], "quota_exhausted")

    def test_empty_or_none_screen_is_safe(self):
        det = QuotaLimitDetector(confirm_polls=1)
        self.assertIsNone(det.poll(""))
        self.assertIsNone(det.poll(None))


class ParseResetTimeTests(unittest.TestCase):
    # Fixed reference: 2026-07-19 13:00 local time
    NOW = time.mktime((2026, 7, 19, 13, 0, 0, -1, -1, -1))

    def test_relative_hours_minutes(self):
        ts = parse_reset_time("Try again in 4 hours 32 minutes.", now=self.NOW)
        self.assertEqual(ts, self.NOW + 4 * 3600 + 32 * 60)

    def test_relative_compact_units(self):
        ts = parse_reset_time("rate limited — retry in 2h 5m", now=self.NOW)
        self.assertEqual(ts, self.NOW + 2 * 3600 + 5 * 60)

    def test_clock_am_rolls_to_tomorrow(self):
        # 3am is already past at 13:00 — expect tomorrow 03:00
        ts = parse_reset_time("5-hour limit reached ∙ resets 3am", now=self.NOW)
        expected = time.mktime((2026, 7, 20, 3, 0, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_clock_pm_today(self):
        ts = parse_reset_time("Your limit will reset at 4:30pm (Asia/Phnom_Penh).",
                              now=self.NOW)
        expected = time.mktime((2026, 7, 19, 16, 30, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_clock_24h(self):
        ts = parse_reset_time("resets at 14:30", now=self.NOW)
        expected = time.mktime((2026, 7, 19, 14, 30, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_date_form(self):
        ts = parse_reset_time("Weekly limit reached · resets Jul 24", now=self.NOW)
        expected = time.mktime((2026, 7, 24, 0, 0, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_date_with_time(self):
        ts = parse_reset_time("resets on Jul 24 at 11:59pm", now=self.NOW)
        expected = time.mktime((2026, 7, 24, 23, 59, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_past_date_rolls_to_next_year(self):
        ts = parse_reset_time("resets Jan 2", now=self.NOW)
        expected = time.mktime((2027, 1, 2, 0, 0, 0, -1, -1, -1))
        self.assertEqual(ts, expected)

    def test_bare_hour_without_am_pm_is_ignored(self):
        self.assertIsNone(parse_reset_time("resets 3", now=self.NOW))

    def test_no_reset_info(self):
        self.assertIsNone(parse_reset_time("You've hit your usage limit.", now=self.NOW))
        self.assertIsNone(parse_reset_time("", now=self.NOW))


class QuotaFlagFileTests(unittest.TestCase):
    def test_exhausted_flag_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_quota_exhausted_flag(
                Path(tmp), "claude",
                "5-hour limit reached ∙ resets 3am",
                "tmux attach -t agentchattr-claude",
                reset_at=1234567890.0, reset_estimated=False)
            flag = Path(tmp) / "claude_quota_exhausted"
            self.assertTrue(flag.exists())
            payload = json.loads(flag.read_text("utf-8"))
            self.assertEqual(payload["agent"], "claude")
            self.assertIn("limit reached", payload["detail"])
            self.assertIn("tmux attach", payload["attach_hint"])
            self.assertEqual(payload["reset_at"], 1234567890.0)
            self.assertFalse(payload["reset_estimated"])

    def test_estimated_reset_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_quota_exhausted_flag(Path(tmp), "claude", "limit",
                                       reset_at=99.0, reset_estimated=True)
            payload = json.loads((Path(tmp) / "claude_quota_exhausted").read_text("utf-8"))
            self.assertTrue(payload["reset_estimated"])

    def test_resolved_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_quota_resolved_flag(Path(tmp), "codex")
            flag = Path(tmp) / "codex_quota_resolved"
            self.assertTrue(flag.exists())
            self.assertEqual(flag.read_text("utf-8"), "codex")


if __name__ == "__main__":
    unittest.main()
