"""Tests for load_config's project-level agentchattr.toml merge.

A project directory (the one `agentchattr up` is run from) can define extra
agents — e.g. a second Antigravity instance "agy2" with its own default
flags — in an `agentchattr.toml` file, and can also retune agents the install
already defines, without touching the shared config.toml/config.local.toml.

Precedence, lowest to highest: config.toml, config.local.toml,
agentchattr.toml. config.local.toml can only ADD names config.toml doesn't
have; agentchattr.toml wins outright. A project override is whole-table —
it REPLACES the upstream agent rather than merging key-by-key, so keys it
omits are dropped, not inherited.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import (  # noqa: E402
    apply_cli_overrides,
    load_config,
    resolve_project_dir,
)


BASE_CONFIG = """
[server]
port = 8300

[agents.claude]
command = "claude"
"""


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        self.root.mkdir()
        (self.root / "config.toml").write_text(BASE_CONFIG, "utf-8")
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()

    def test_no_project_file_is_a_no_op(self):
        config = load_config(self.root, project_dir=self.project)
        self.assertEqual(set(config["agents"]), {"claude"})

    def test_project_agent_is_merged_in(self):
        (self.project / "agentchattr.toml").write_text(
            '[agents.agy2]\ncommand = "agy"\nargs = ["--dangerously-skip-permissions"]\n',
            "utf-8",
        )
        config = load_config(self.root, project_dir=self.project)
        self.assertEqual(set(config["agents"]), {"claude", "agy2"})
        self.assertEqual(config["agents"]["agy2"]["args"], ["--dangerously-skip-permissions"])

    def test_project_overrides_an_existing_agent(self):
        (self.project / "agentchattr.toml").write_text(
            '[agents.claude]\ncommand = "not-claude"\n',
            "utf-8",
        )
        config = load_config(self.root, project_dir=self.project)
        self.assertEqual(config["agents"]["claude"]["command"], "not-claude")

    def test_project_override_replaces_whole_table(self):
        """Override is whole-table: upstream keys the project omits are dropped."""
        (self.root / "config.toml").write_text(
            '[agents.agy]\ncommand = "agy"\nmcp_inject = "settings_file"\n'
            'color = "#00bcd4"\n',
            "utf-8",
        )
        (self.project / "agentchattr.toml").write_text(
            '[agents.agy]\ncommand = "agy"\nmodel = "gemini-3-pro"\n',
            "utf-8",
        )
        agy = load_config(self.root, project_dir=self.project)["agents"]["agy"]
        self.assertEqual(agy["model"], "gemini-3-pro")
        self.assertNotIn("mcp_inject", agy)
        self.assertNotIn("color", agy)

    def test_no_project_dir_skips_project_file_entirely(self):
        (self.project / "agentchattr.toml").write_text(
            '[agents.agy2]\ncommand = "agy"\n', "utf-8",
        )
        config = load_config(self.root)  # project_dir=None
        self.assertEqual(set(config["agents"]), {"claude"})

    def test_project_takes_precedence_over_local(self):
        (self.root / "config.local.toml").write_text(
            '[agents.agy2]\ncommand = "from-local"\n', "utf-8",
        )
        (self.project / "agentchattr.toml").write_text(
            '[agents.agy2]\ncommand = "from-project"\n', "utf-8",
        )
        config = load_config(self.root, project_dir=self.project)
        self.assertEqual(config["agents"]["agy2"]["command"], "from-project")

    def test_local_still_cannot_override_install_config(self):
        (self.root / "config.local.toml").write_text(
            '[agents.claude]\ncommand = "from-local"\n', "utf-8",
        )
        config = load_config(self.root)
        self.assertEqual(config["agents"]["claude"]["command"], "claude")


class ResolveProjectDirTests(unittest.TestCase):
    """AGENTCHATTR_CWD → project dir, the path both the server and wrappers take.

    They must agree: the server seeds its instance registry from the merged
    roster, so an agent the server resolved differently (or not at all) rejects
    the wrapper's registration with "unknown base".
    """

    def test_unset_is_none(self):
        with mock.patch.dict(os.environ, {"AGENTCHATTR_CWD": ""}, clear=False):
            self.assertIsNone(resolve_project_dir())

    def test_absolute_path_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {"AGENTCHATTR_CWD": str(resolved)}):
                self.assertEqual(resolve_project_dir(), resolved)

    def test_relative_path_resolves_against_cwd(self):
        with mock.patch.dict(os.environ, {"AGENTCHATTR_CWD": "sub/dir"}):
            self.assertEqual(resolve_project_dir(), (Path.cwd() / "sub/dir").resolve())

    def test_cwd_flag_feeds_the_resolver(self):
        """`--cwd` (as run.py and wrapper.py are launched with) reaches the resolver."""
        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENTCHATTR_CWD", None)
                apply_cli_overrides(["run.py", "--port", "9999", "--cwd", str(resolved)])
                self.assertEqual(resolve_project_dir(), resolved)


class ServerSeedsProjectAgentsTests(unittest.TestCase):
    """The registry the server seeds must contain project-defined agents.

    Regression test for project agents failing to start: the server loaded
    config without a project dir, so a wrapper launching `agy2` from a project
    agentchattr.toml registered against a registry that had never heard of it
    and exited on HTTP 400.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "install"
        self.root.mkdir()
        (self.root / "config.toml").write_text(BASE_CONFIG, "utf-8")
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / "agentchattr.toml").write_text(
            '[agents.agy2]\ncommand = "agy"\ncolor = "#060276"\nlabel = "antigravity-2"\n',
            "utf-8",
        )

    def _seeded_registry(self, project_dir):
        from registry import RuntimeRegistry
        config = load_config(self.root, project_dir=project_dir)
        registry = RuntimeRegistry(data_dir=str(Path(self.tmp.name) / "data"))
        registry.seed(config.get("agents", {}))
        return registry

    def test_project_agent_can_register(self):
        registry = self._seeded_registry(self.project)
        result = registry.register("agy2")
        self.assertIsNotNone(result, "project agent was rejected as an unknown base")
        self.assertEqual(result["name"], "antigravity-2")

    def test_project_agent_keeps_its_color_and_label(self):
        """Project-defined color/label reach the registry, not just launch args."""
        registry = self._seeded_registry(self.project)
        result = registry.register("agy2")
        self.assertEqual(result["color"], "#060276")
        self.assertEqual(result["label"], "antigravity-2")

    def test_without_project_dir_the_agent_is_unknown(self):
        """Pins the old behaviour that caused the bug, so a regression is visible."""
        registry = self._seeded_registry(None)
        self.assertIsNone(registry.register("agy2"))


class AgentEnvTests(unittest.TestCase):
    """`[agents.NAME.env]` — per-agent environment, chiefly a separate HOME.

    The agent is exec'd directly with no shell, so `command = "HOME=... agy"`
    can't work: the whole string is looked up as one executable. The env table
    is the supported way, and settings paths must follow the HOME it sets.
    """

    def test_env_table_parses_as_a_plain_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.toml").write_text(
                BASE_CONFIG + '\n[agents.agy2]\ncommand = "agy"\n'
                '\n[agents.agy2.env]\nHOME = "/home/you/.agy-profiles/agy2"\n',
                "utf-8",
            )
            agy2 = load_config(root)["agents"]["agy2"]
            self.assertEqual(agy2["command"], "agy")
            self.assertEqual(agy2["env"], {"HOME": "/home/you/.agy-profiles/agy2"})

    def test_tilde_expands_against_the_agents_home(self):
        from wrapper import _expanduser_in
        env = {"HOME": "/home/you/.agy-profiles/agy2"}
        self.assertEqual(
            _expanduser_in("~/.gemini/config/mcp_config.json", env),
            Path("/home/you/.agy-profiles/agy2/.gemini/config/mcp_config.json"),
        )

    def test_bare_tilde_expands_to_the_agents_home(self):
        from wrapper import _expanduser_in
        self.assertEqual(_expanduser_in("~", {"HOME": "/home/you/p"}), Path("/home/you/p"))

    def test_absolute_path_is_untouched(self):
        from wrapper import _expanduser_in
        env = {"HOME": "/home/you/.agy-profiles/agy2"}
        self.assertEqual(_expanduser_in("/etc/mcp.json", env), Path("/etc/mcp.json"))

    def test_no_home_in_env_falls_back_to_normal_expansion(self):
        from wrapper import _expanduser_in
        self.assertEqual(_expanduser_in("~/x", {}), Path("~/x").expanduser())
        self.assertEqual(_expanduser_in("~/x", None), Path("~/x").expanduser())

    def test_tilde_inside_a_path_is_not_treated_as_home(self):
        from wrapper import _expanduser_in
        env = {"HOME": "/home/you/p"}
        self.assertEqual(_expanduser_in("cfg/~backup.json", env), Path("cfg/~backup.json"))


if __name__ == "__main__":
    unittest.main()
