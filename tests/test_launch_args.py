"""Tests for resolve_launch_args — named modes and model/effort resolution.

Exercised directly rather than through wrapper.py because the wrapper only
concatenates the result with argv passthrough; the precedence rules and the
flag-template rendering are the parts worth pinning down.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_config, resolve_launch_args  # noqa: E402


CODEX = {
    "command": "codex",
    "model_flag_template": "--model {model}",
    "effort_flag_template": '-c model_reasoning_effort="{effort}"',
    "modes": {"bypass": {"args": ["--dangerously-bypass-approvals-and-sandbox"]}},
}


class ModeTests(unittest.TestCase):
    def test_no_mode_yields_nothing(self):
        args, warnings = resolve_launch_args("codex", CODEX)
        self.assertEqual(args, [])
        self.assertEqual(warnings, [])

    def test_mode_contributes_args(self):
        args, _ = resolve_launch_args("codex", CODEX, mode="bypass")
        self.assertEqual(args, ["--dangerously-bypass-approvals-and-sandbox"])

    def test_unknown_mode_raises_and_lists_options(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_launch_args("codex", CODEX, mode="nope")
        self.assertIn("bypass", str(ctx.exception))

    def test_agent_args_precede_mode_args(self):
        cfg = {**CODEX, "args": ["--base"]}
        args, _ = resolve_launch_args("codex", cfg, mode="bypass")
        self.assertEqual(args, ["--base", "--dangerously-bypass-approvals-and-sandbox"])


class ModelEffortTests(unittest.TestCase):
    def test_templates_render(self):
        args, warnings = resolve_launch_args("codex", CODEX, model="gpt-5", effort="high")
        self.assertEqual(args, ["--model", "gpt-5", "-c", 'model_reasoning_effort="high"'])
        self.assertEqual(warnings, [])

    def test_effort_quoting_survives(self):
        """codex needs the quotes to parse the value as TOML, so .split() not shlex."""
        args, _ = resolve_launch_args("codex", CODEX, effort="high")
        self.assertIn('model_reasoning_effort="high"', args)

    def test_explicit_override_beats_mode_beats_agent(self):
        cfg = {**CODEX, "model": "from-agent",
               "modes": {"deep": {"model": "from-mode"}}}
        args, _ = resolve_launch_args("codex", cfg, mode="deep")
        self.assertEqual(args, ["--model", "from-mode"])
        args, _ = resolve_launch_args("codex", cfg, mode="deep", model="from-flag")
        self.assertEqual(args, ["--model", "from-flag"])
        args, _ = resolve_launch_args("codex", cfg)
        self.assertEqual(args, ["--model", "from-agent"])

    def test_missing_template_warns_rather_than_dropping(self):
        cfg = {"command": "claude", "model_flag_template": "--model {model}"}
        args, warnings = resolve_launch_args("claude", cfg, model="opus", effort="high")
        self.assertEqual(args, ["--model", "opus"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("effort_flag_template", warnings[0])

    def test_api_agent_model_is_not_turned_into_a_flag(self):
        """type='api' agents send `model` in the request body, not on a CLI."""
        cfg = {"type": "api", "model": "MiniMax-M3"}
        args, warnings = resolve_launch_args("minimax", cfg)
        self.assertEqual(args, [])
        self.assertEqual(warnings, [])


class ShippedConfigTests(unittest.TestCase):
    """The modes in config.toml must match the flags the start_*.sh scripts use."""

    def setUp(self):
        self.agents = load_config(ROOT).get("agents", {})

    def test_claude_yolo_matches_legacy_launcher(self):
        args, _ = resolve_launch_args("claude", self.agents["claude"], mode="yolo")
        self.assertEqual(args, ["--dangerously-skip-permissions"])

    def test_codex_bypass_matches_legacy_launcher(self):
        args, _ = resolve_launch_args("codex", self.agents["codex"], mode="bypass")
        self.assertEqual(args, ["--dangerously-bypass-approvals-and-sandbox"])

    def test_gemini_yolo_matches_legacy_launcher(self):
        args, _ = resolve_launch_args("gemini", self.agents["gemini"], mode="yolo")
        self.assertEqual(args, ["--yolo"])

    def test_every_shipped_mode_is_loadable(self):
        for name, cfg in self.agents.items():
            for mode in cfg.get("modes", {}):
                with self.subTest(agent=name, mode=mode):
                    resolve_launch_args(name, cfg, mode=mode)


if __name__ == "__main__":
    unittest.main()
