"""Tests for agentchattr_cli.py — per-project slug, state, and port allocation.

These are pure state/logic tests: no tmux sessions or servers are started.
"""

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agentchattr_cli as cli  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_slug_is_deterministic(self):
        p = Path("/home/user/projects/myapp")
        self.assertEqual(cli.slug_for(p), cli.slug_for(p))

    def test_slug_differs_for_same_basename_different_path(self):
        a = cli.slug_for(Path("/home/user/projects/myapp"))
        b = cli.slug_for(Path("/srv/other/myapp"))
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("myapp-"))
        self.assertTrue(b.startswith("myapp-"))

    def test_slug_sanitizes_odd_names(self):
        slug = cli.slug_for(Path("/tmp/My Project (v2)!"))
        self.assertRegex(slug, r"^[a-z0-9-]+-[0-9a-f]{8}$")


class InstanceStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = cli.INSTANCES_DIR
        cli.INSTANCES_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(cli, "INSTANCES_DIR", self._saved))

    def test_save_and_load_roundtrip(self):
        state = {"version": 1, "slug": "myapp-1a2b3c4d",
                 "ports": {"server": 8460, "mcp_http": 8461, "mcp_sse": 8462}}
        cli.save_state("myapp-1a2b3c4d", state)
        self.assertEqual(cli.load_state("myapp-1a2b3c4d"), state)

    def test_load_missing_returns_none(self):
        self.assertIsNone(cli.load_state("nope-00000000"))

    def test_corrupt_state_returns_none(self):
        path = cli.state_path("bad-00000000")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIsNone(cli.load_state("bad-00000000"))

    def test_all_states_enumerates_instances(self):
        cli.save_state("a-11111111", {"slug": "a-11111111"})
        cli.save_state("b-22222222", {"slug": "b-22222222"})
        slugs = {s["slug"] for s in cli._all_states()}
        self.assertEqual(slugs, {"a-11111111", "b-22222222"})


class PortAllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = cli.INSTANCES_DIR
        cli.INSTANCES_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(cli, "INSTANCES_DIR", self._saved))

    def test_allocation_is_deterministic_and_in_range(self):
        project = Path("/home/user/projects/myapp")
        first = cli.allocate_ports(project, "myapp-1a2b3c4d")
        second = cli.allocate_ports(project, "myapp-1a2b3c4d")
        self.assertEqual(first, second)
        self.assertGreaterEqual(first["server"], cli.PORT_BASE)
        self.assertEqual(first["mcp_http"], first["server"] + 1)
        self.assertEqual(first["mcp_sse"], first["server"] + 2)
        # Never collides with the stock single-instance ports.
        self.assertNotIn(first["server"], (8200, 8201, 8300))

    def test_ports_claimed_by_other_instance_are_skipped(self):
        project = Path("/home/user/projects/myapp")
        natural = cli.allocate_ports(project, "myapp-1a2b3c4d")
        cli.save_state("other-99999999", {
            "slug": "other-99999999",
            "ports": natural,
        })
        stepped = cli.allocate_ports(project, "myapp-1a2b3c4d")
        self.assertNotEqual(stepped["server"], natural["server"])
        self.assertFalse(set(stepped.values()) & set(natural.values()))

    def test_bound_port_is_skipped(self):
        project = Path("/home/user/projects/myapp")
        natural = cli.allocate_ports(project, "myapp-1a2b3c4d")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", natural["server"]))
            blocker.listen(1)
            stepped = cli.allocate_ports(project, "myapp-1a2b3c4d")
        self.assertNotEqual(stepped["server"], natural["server"])

    def test_own_slug_ports_not_treated_as_claimed(self):
        project = Path("/home/user/projects/myapp")
        natural = cli.allocate_ports(project, "myapp-1a2b3c4d")
        cli.save_state("myapp-1a2b3c4d", {"slug": "myapp-1a2b3c4d", "ports": natural})
        again = cli.allocate_ports(project, "myapp-1a2b3c4d")
        self.assertEqual(again, natural)


class SessionNameParsingTests(unittest.TestCase):
    def test_wrapper_session_pattern(self):
        inst = cli.Instance(Path("/home/user/projects/myapp"))
        import re
        pattern = re.compile(rf"^{re.escape(inst.prefix)}-w(\d+)-(.+)$")
        m = pattern.match(f"{inst.prefix}-w3-agy")
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 3)
        self.assertEqual(m.group(2), "agy")
        # Agent TUI sessions (server-assigned names) must NOT match.
        self.assertIsNone(pattern.match(f"{inst.prefix}-agy-2"))
        self.assertIsNone(pattern.match(f"{inst.prefix}-server"))


if __name__ == "__main__":
    unittest.main()
