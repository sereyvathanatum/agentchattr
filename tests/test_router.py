import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router


class RouterMentionTests(unittest.TestCase):
    def test_agent_can_mention_another_agent_by_display_label(self):
        router = Router(
            ["chatgpt", "agy2"],
            default_mention="none",
            mention_aliases={"antigravity-2": "agy2"},
        )

        self.assertEqual(
            router.get_targets(
                "chatgpt",
                "@antigravity-2 taskctl has 2 runnable task(s)",
            ),
            ["agy2"],
        )

    def test_canonical_name_wins_over_colliding_display_label(self):
        router = Router(
            ["alpha", "beta"],
            default_mention="none",
            mention_aliases={"alpha": "beta"},
        )

        self.assertEqual(router.get_targets("user", "@alpha go"), ["alpha"])

    def test_hyphenated_agent_name_is_parsed_as_full_mention(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("please ask @telegram-bridge to check")),
            {"telegram-bridge"},
        )

    def test_shorter_agent_name_does_not_match_prefix_of_hyphenated_unknown(self):
        router = Router(["telegram"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bridge check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bridge check"), [])

    def test_longest_hyphenated_name_wins_when_prefix_agent_also_exists(self):
        router = Router(["telegram", "telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge check")),
            {"telegram-bridge"},
        )

    def test_unknown_exact_handle_still_does_not_route(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bot check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bot check"), [])


class RouterLoopGuardTests(unittest.TestCase):
    def test_guard_pauses_after_max_hops(self):
        router = Router(["a", "b"], default_mention="none", max_hops=2)

        # Each agent-to-agent hop counts; the hop past max_hops pauses.
        self.assertEqual(router.get_targets("a", "@b hi"), ["b"])
        self.assertEqual(router.get_targets("b", "@a hi"), ["a"])
        self.assertEqual(router.get_targets("a", "@b hi"), [])
        self.assertTrue(router.is_paused())

    def test_disabled_guard_never_pauses(self):
        router = Router(["a", "b"], default_mention="none", max_hops=2,
                        guard_enabled=False)

        for _ in range(10):
            self.assertEqual(router.get_targets("a", "@b hi"), ["b"])
            self.assertEqual(router.get_targets("b", "@a hi"), ["a"])
        self.assertFalse(router.is_paused())

    def test_guard_can_be_toggled_off_at_runtime(self):
        router = Router(["a", "b"], default_mention="none", max_hops=2)

        router.guard_enabled = False
        for _ in range(10):
            router.get_targets("a", "@b hi")
            router.get_targets("b", "@a hi")
        self.assertFalse(router.is_paused())


if __name__ == "__main__":
    unittest.main()
