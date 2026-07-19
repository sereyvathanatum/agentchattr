import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from login_detect import (
    LoginPromptDetector,
    write_login_required_flag,
    write_login_resolved_flag,
)

CLAUDE_LOGIN = """\
Welcome to Claude Code

 Select login method:

 > 1. Claude account with subscription
   2. Anthropic Console account
"""

CLAUDE_EXPIRED = "Invalid API key · Please run /login"

CODEX_LOGIN = """\
Welcome to Codex

  Sign in with ChatGPT to get started
"""

GEMINI_DEVICE_CODE = """\
Waiting for auth... (Press ESC to cancel)
Enter the one-time code shown below:
"""

NORMAL_SCREEN = """\
> analyzing router.py

I found the bug in parse_mentions — fixing it now.
"""


class LoginPromptDetectorTests(unittest.TestCase):
    def test_detects_claude_login_screen_after_confirm_polls(self):
        det = LoginPromptDetector(confirm_polls=2)
        self.assertIsNone(det.poll(CLAUDE_LOGIN))
        event = det.poll(CLAUDE_LOGIN)
        self.assertIsNotNone(event)
        kind, detail = event
        self.assertEqual(kind, "login_required")
        self.assertIn("Select login method", detail)

    def test_detects_expired_session_prompt(self):
        det = LoginPromptDetector(confirm_polls=1)
        kind, detail = det.poll(CLAUDE_EXPIRED)
        self.assertEqual(kind, "login_required")
        self.assertIn("/login", detail)

    def test_detects_codex_and_device_code_screens(self):
        for screen in (CODEX_LOGIN, GEMINI_DEVICE_CODE):
            det = LoginPromptDetector(confirm_polls=1)
            event = det.poll(screen)
            self.assertIsNotNone(event, screen)
            self.assertEqual(event[0], "login_required")

    def test_single_poll_blip_does_not_trigger(self):
        det = LoginPromptDetector(confirm_polls=2)
        self.assertIsNone(det.poll(CLAUDE_LOGIN))
        self.assertIsNone(det.poll(NORMAL_SCREEN))  # resets streak
        self.assertIsNone(det.poll(CLAUDE_LOGIN))

    def test_normal_output_never_triggers(self):
        det = LoginPromptDetector(confirm_polls=1)
        for _ in range(5):
            self.assertIsNone(det.poll(NORMAL_SCREEN))

    def test_no_repeat_notification_while_prompt_persists(self):
        det = LoginPromptDetector(confirm_polls=1)
        self.assertEqual(det.poll(CLAUDE_LOGIN)[0], "login_required")
        for _ in range(10):
            self.assertIsNone(det.poll(CLAUDE_LOGIN))

    def test_resolved_after_clear_polls_then_can_renotify(self):
        det = LoginPromptDetector(confirm_polls=1, clear_polls=3)
        self.assertEqual(det.poll(CLAUDE_LOGIN)[0], "login_required")
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        event = det.poll(NORMAL_SCREEN)
        self.assertEqual(event, ("resolved", ""))
        # A later session timeout notifies again
        self.assertEqual(det.poll(CLAUDE_EXPIRED)[0], "login_required")

    def test_redraw_flicker_does_not_resolve_early(self):
        det = LoginPromptDetector(confirm_polls=1, clear_polls=3)
        det.poll(CLAUDE_LOGIN)
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        self.assertIsNone(det.poll(CLAUDE_LOGIN))  # resets miss streak
        self.assertIsNone(det.poll(NORMAL_SCREEN))
        self.assertIsNone(det.poll(NORMAL_SCREEN))

    def test_extra_patterns_from_config(self):
        det = LoginPromptDetector(extra_patterns=[r"scan the qr code"],
                                  confirm_polls=1)
        event = det.poll("Please Scan the QR Code with your phone")
        self.assertEqual(event[0], "login_required")

    def test_empty_or_none_screen_is_safe(self):
        det = LoginPromptDetector(confirm_polls=1)
        self.assertIsNone(det.poll(""))
        self.assertIsNone(det.poll(None))


class LoginFlagFileTests(unittest.TestCase):
    def test_login_required_flag_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_login_required_flag(Path(tmp), "claude",
                                      "Please run /login",
                                      "tmux attach -t agentchattr-claude")
            flag = Path(tmp) / "claude_login_required"
            self.assertTrue(flag.exists())
            payload = json.loads(flag.read_text("utf-8"))
            self.assertEqual(payload["agent"], "claude")
            self.assertEqual(payload["detail"], "Please run /login")
            self.assertIn("tmux attach", payload["attach_hint"])

    def test_login_resolved_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_login_resolved_flag(Path(tmp), "codex")
            flag = Path(tmp) / "codex_login_resolved"
            self.assertTrue(flag.exists())
            self.assertEqual(flag.read_text("utf-8"), "codex")


if __name__ == "__main__":
    unittest.main()
