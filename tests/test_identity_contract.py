import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import mcp_bridge
import mcp_proxy
import wrapper
import wrapper_unix
if sys.platform == "win32":
    import wrapper_windows
from mcp.server.fastmcp import Context
from registry import RuntimeRegistry
from store import MessageStore


class FakeRequest:
    def __init__(self, headers=None, body=None):
        self.headers = headers or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def auth_ctx(token: str) -> Context:
    request = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    request_context = SimpleNamespace(request=request)
    return Context(request_context=request_context)


class RuntimeRegistryTests(unittest.TestCase):
    def test_registers_active_instances_and_resolves_current_name_from_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeRegistry(data_dir=tmp)
            registry.seed({"claude": {"label": "Claude", "color": "#da7756"}})

            first = registry.register("claude")
            second = registry.register("claude")

            self.assertEqual(first["state"], "active")
            self.assertEqual(second["state"], "active")
            self.assertEqual(second["name"], "claude-2")

            resolved_first = registry.resolve_token(first["token"])
            self.assertIsNotNone(resolved_first)
            self.assertEqual(resolved_first["name"], "claude-1")
            self.assertEqual(resolved_first["identity_id"], first["identity_id"])
            self.assertEqual(resolved_first["epoch"], 1)


class McpBridgeAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.registry = RuntimeRegistry(data_dir=self.tmp.name)
        self.registry.seed(
            {
                "codex": {"label": "Codex", "color": "#10a37f"},
                "claude": {"label": "Claude", "color": "#da7756"},
            }
        )
        self.store = MessageStore(str(Path(self.tmp.name) / "messages.jsonl"))

        mcp_bridge.store = self.store
        mcp_bridge.registry = self.registry
        mcp_bridge.decisions = None
        mcp_bridge.room_settings = {"channels": ["general"]}
        mcp_bridge.config = {"images": {"upload_dir": str(Path(self.tmp.name) / "uploads")}}
        mcp_bridge._presence.clear()
        mcp_bridge._activity.clear()
        mcp_bridge._cursors.clear()
        mcp_bridge._renamed_from.clear()

    def test_chat_send_stamps_authenticated_sender(self):
        self.registry.register("codex")
        second = self.registry.register("codex")

        result = mcp_bridge.chat_send(
            sender="claude",
            message="hello from the right identity",
            ctx=auth_ctx(second["token"]),
        )

        self.assertIn("Sent", result)
        recent = self.store.get_recent(1)
        self.assertEqual(recent[0]["sender"], second["name"])

    def test_chat_send_rejects_unauthenticated_agent_sender(self):
        self.registry.register("codex")

        result = mcp_bridge.chat_send(sender="codex", message="no auth")

        self.assertIn("authenticated agent session required", result)

    def test_stale_token_is_rejected_after_deregister(self):
        inst = self.registry.register("codex")
        self.registry.deregister(inst["name"])

        result = mcp_bridge.chat_send(
            sender=inst["name"],
            message="stale",
            ctx=auth_ctx(inst["token"]),
        )

        self.assertIn("stale or unknown authenticated agent session", result)


class AppAuthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.registry = RuntimeRegistry(data_dir=self.tmp.name)
        self.registry.seed({"claude": {"label": "Claude", "color": "#da7756"}})
        self.store = MessageStore(str(Path(self.tmp.name) / "messages.jsonl"))

        app.registry = self.registry
        app.store = self.store
        mcp_bridge.store = self.store
        mcp_bridge.registry = self.registry
        mcp_bridge._presence.clear()
        mcp_bridge._activity.clear()
        mcp_bridge._cursors.clear()
        mcp_bridge._renamed_from.clear()

    def test_heartbeat_requires_valid_token_for_registered_agent(self):
        inst = self.registry.register("claude")

        ok = asyncio.run(
            app.heartbeat(
                "claude",
                FakeRequest(headers={"authorization": f"Bearer {inst['token']}"}),
            )
        )
        self.assertEqual(ok["name"], inst["name"])

        stale = asyncio.run(
            app.heartbeat(
                "claude",
                FakeRequest(headers={"authorization": "Bearer deadbeef"}),
            )
        )
        self.assertEqual(stale.status_code, 409)

    def test_deregister_uses_authenticated_identity_not_path_text(self):
        inst = self.registry.register("claude")

        resp = asyncio.run(
            app.deregister_agent(
                "wrong-name",
                FakeRequest(headers={"authorization": f"Bearer {inst['token']}"}),
            )
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.registry.get_instance(inst["name"]))


class WrapperLaunchTests(unittest.TestCase):
    def test_build_provider_launch_for_claude_uses_direct_server_auth(self):
        """Claude bypasses proxy — connects directly to MCP server with bearer token."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a fake project .mcp.json so _read_project_mcp_servers works
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            (project_dir / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "unity-mcp": {"type": "http", "url": "http://127.0.0.1:8090/mcp"},
                    "agentchattr": {"type": "http", "url": "http://127.0.0.1:8200/mcp"},
                }
            }))

            args, env, inject_env, _ = wrapper._build_provider_launch(
                agent="claude",
                agent_cfg={},
                instance_name="claude-2",
                data_dir=Path(tmp),
                proxy_url=None,
                extra_args=["--debug"],
                env={"PATH": os.environ.get("PATH", "")},
                token="test_token_abc",
                mcp_cfg={"http_port": 8200},
                project_dir=project_dir,
            )

            self.assertEqual(args[0], "--mcp-config")
            config_path = Path(args[1])
            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text("utf-8"))
            # Points at real server, not proxy
            self.assertEqual(
                payload["mcpServers"]["agentchattr"]["url"],
                "http://127.0.0.1:8200/mcp",
            )
            # Bearer token in headers
            self.assertEqual(
                payload["mcpServers"]["agentchattr"]["headers"]["Authorization"],
                "Bearer test_token_abc",
            )
            # Project servers preserved (minus unauthenticated agentchattr)
            self.assertIn("unity-mcp", payload["mcpServers"])
            # Extra args preserved
            self.assertEqual(args[2], "--debug")
            self.assertEqual(env["PATH"], os.environ.get("PATH", ""))

    def test_build_provider_launch_for_gemini_uses_direct_server_auth(self):
        """Gemini bypasses proxy — connects directly to the streamable-http MCP
        server with a bearer token. Transport is http (not sse): SSE has blocking
        issues in Gemini 0.32.x, so the built-in default was switched to http."""
        with tempfile.TemporaryDirectory() as tmp:
            args, env, inject_env, _ = wrapper._build_provider_launch(
                agent="gemini",
                agent_cfg={},
                instance_name="gemini-2",
                data_dir=Path(tmp),
                proxy_url=None,
                extra_args=[],
                env={},
                token="gemini_token_xyz",
                mcp_cfg={"http_port": 8200},
            )

            self.assertEqual(args, [])
            # Settings path is in inject_env (propagated through tmux on Mac/Linux)
            self.assertIn("GEMINI_CLI_SYSTEM_SETTINGS_PATH", inject_env)
            self.assertNotIn("GEMINI_CLI_SYSTEM_SETTINGS_PATH", env)
            settings_path = Path(inject_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
            self.assertTrue(settings_path.exists())
            payload = json.loads(settings_path.read_text("utf-8"))
            self.assertEqual(payload["mcpServers"]["agentchattr"]["type"], "http")
            # Points at the real streamable-http server, not the proxy. Gemini
            # expects the URL under "httpUrl" (not "url") for http transport.
            self.assertEqual(
                payload["mcpServers"]["agentchattr"]["httpUrl"],
                "http://127.0.0.1:8200/mcp",
            )
            # Bearer token in headers
            self.assertEqual(
                payload["mcpServers"]["agentchattr"]["headers"]["Authorization"],
                "Bearer gemini_token_xyz",
            )

    def test_build_provider_launch_for_codex_uses_config_override(self):
        args, env, inject_env, _ = wrapper._build_provider_launch(
            agent="codex",
            agent_cfg={},
            instance_name="codex-2",
            data_dir=Path(tempfile.gettempdir()),
            proxy_url="http://127.0.0.1:7777/mcp",
            extra_args=["--no-alt-screen"],
            env={"PATH": os.environ.get("PATH", "")},
        )

        self.assertEqual(args[0], "-c")
        self.assertIn('mcp_servers.agentchattr.url="http://127.0.0.1:7777/mcp"', args[1])
        self.assertEqual(args[2], "--no-alt-screen")
        self.assertIn("PATH", env)


@unittest.skipIf(sys.platform == "win32", "PTY agent host is Mac/Linux only")
class AgentTerminalTests(unittest.TestCase):
    """The pyte-backed screen the login/quota watchers poll."""

    def test_screen_is_none_until_the_agent_starts(self):
        terminal = wrapper_unix.AgentTerminal(cols=40, rows=4)
        # Detectors must not run against a blank screen before launch —
        # "no screen" and "an empty screen" mean different things to them.
        self.assertIsNone(terminal.read_screen())

    def test_screen_renders_escape_sequences_away(self):
        terminal = wrapper_unix.AgentTerminal(cols=40, rows=4)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        terminal.attach_pty(write_fd)
        self.addCleanup(os.close, write_fd)

        terminal.feed(b"Please \x1b[31mlog in\x1b[0m to continue\r\nsecond line")

        screen = terminal.read_screen()
        self.assertIn("Please log in to continue", screen)
        self.assertIn("second line", screen)
        self.assertNotIn("\x1b", screen)

    def test_screen_reflects_only_what_is_currently_displayed(self):
        # The whole reason for emulating a terminal rather than buffering the
        # output stream: a cleared prompt has to actually leave the screen, or
        # the watchers never report "resolved".
        terminal = wrapper_unix.AgentTerminal(cols=40, rows=4)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        terminal.attach_pty(write_fd)
        self.addCleanup(os.close, write_fd)

        terminal.feed(b"session has expired")
        self.assertIn("session has expired", terminal.read_screen())

        terminal.feed(b"\x1b[2J\x1b[H")  # clear screen, home cursor
        self.assertNotIn("session has expired", terminal.read_screen())

    def test_inject_types_the_text_then_presses_enter(self):
        terminal = wrapper_unix.AgentTerminal(cols=40, rows=4)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        terminal.attach_pty(write_fd)
        self.addCleanup(os.close, write_fd)

        terminal.inject("hello agent", delay=0)
        self.assertEqual(os.read(read_fd, 1024), b"hello agent\r")

        # Enter must be its own write: agent CLIs consume the prompt as it
        # arrives, and a newline in the same write can submit early.
        writes = []
        with mock.patch.object(terminal, "write", side_effect=lambda d: writes.append(d) or True):
            terminal.inject("second prompt", delay=0)
        self.assertEqual(writes, [b"second prompt", b"\r"])

    def test_activity_checker_reports_screen_changes_and_triggers(self):
        terminal = wrapper_unix.AgentTerminal(cols=40, rows=4)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        terminal.attach_pty(write_fd)
        self.addCleanup(os.close, write_fd)

        trigger = [False]
        check = wrapper_unix.get_activity_checker(terminal, trigger_flag=trigger)

        self.assertFalse(check())  # first poll only seeds the baseline
        terminal.feed(b"thinking...")
        self.assertTrue(check())
        self.assertFalse(check())  # unchanged screen is not activity

        trigger[0] = True
        self.assertTrue(check())
        self.assertFalse(trigger[0])


@unittest.skipIf(sys.platform == "win32", "PTY agent host is Mac/Linux only")
class WrapperUnixLifecycleTests(unittest.TestCase):
    """run_agent hosts the agent CLI directly on a PTY it owns."""

    def _script(self, body: str) -> str:
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        path = Path(tmpdir) / "fake_agent.sh"
        path.write_text("#!/bin/bash\n" + body, "utf-8")
        path.chmod(0o755)
        return str(path)

    def test_agent_receives_argv_and_env_without_a_shell_round_trip(self):
        out = Path(tempfile.mkdtemp()) / "argv.txt"
        self.addCleanup(lambda: __import__("shutil").rmtree(out.parent, ignore_errors=True))
        command = self._script(
            f'for a in "$@"; do printf "[%s]\\n" "$a" >> {out}; done\n'
            f'printf "STRIPPED=[%s]\\n" "$STRIPPED" >> {out}\n'
            f'printf "INJECTED=[%s]\\n" "$INJECTED" >> {out}\n'
            f'printf "CWD=[%s]\\n" "$PWD" >> {out}\n'
        )
        cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(cwd, ignore_errors=True))

        wrapper_unix.run_agent(
            command=command,
            # codex's effort flag is the argument most likely to be mangled by
            # a quoting round-trip: the inner quotes have to survive verbatim.
            extra_args=["--model", "opus", "-c", 'model_reasoning_effort="high"'],
            cwd=cwd,
            env={"PATH": os.environ["PATH"], "STRIPPED": "should-be-gone"},
            queue_file=Path("queue.jsonl"),
            agent="claude",
            no_restart=True,
            start_watcher=lambda inject_fn: None,
            strip_env=["STRIPPED"],
            inject_env={"INJECTED": "value with spaces"},
        )

        written = out.read_text("utf-8")
        self.assertIn("[--model]\n[opus]\n[-c]\n[model_reasoning_effort=\"high\"]", written)
        self.assertIn("STRIPPED=[]", written)
        self.assertIn("INJECTED=[value with spaces]", written)
        self.assertIn(f"CWD=[{Path(cwd).resolve()}]", written)

    def test_agent_output_lands_on_the_shared_screen(self):
        terminal = wrapper_unix.AgentTerminal(cols=60, rows=6)
        command = self._script('printf "agent is up\\n"\nsleep 0.3\n')
        screens = []

        def capture(inject_fn):
            # start_watcher fires once the terminal is live, which is when the
            # wrapper's watcher threads begin polling for real.
            def poll():
                for _ in range(40):
                    time.sleep(0.05)
                    screen = terminal.read_screen()
                    if screen and "agent is up" in screen:
                        screens.append(screen)
                        return
            threading.Thread(target=poll, daemon=True).start()

        wrapper_unix.run_agent(
            command=command, extra_args=[], cwd=".", env={"PATH": os.environ["PATH"]},
            queue_file=Path("queue.jsonl"), agent="claude", no_restart=True,
            start_watcher=capture, terminal=terminal,
        )

        self.assertTrue(screens, "agent output never reached the pyte screen")

    def test_exited_agent_is_restarted_until_no_restart(self):
        counter = Path(tempfile.mkdtemp()) / "runs"
        self.addCleanup(
            lambda: __import__("shutil").rmtree(counter.parent, ignore_errors=True))
        command = self._script(f'printf "x" >> {counter}\n')

        sleeps = []
        real_sleep = time.sleep

        def fake_sleep(seconds):
            sleeps.append(seconds)
            # Third launch is enough to prove the restart loop runs; break out
            # rather than letting it spin for the real 3s backoff each time.
            if counter.exists() and len(counter.read_text()) >= 3:
                raise KeyboardInterrupt
            real_sleep(min(seconds, 0.01))

        with mock.patch.object(wrapper_unix.time, "sleep", side_effect=fake_sleep):
            wrapper_unix.run_agent(
                command=command, extra_args=[], cwd=".", env={"PATH": os.environ["PATH"]},
                queue_file=Path("queue.jsonl"), agent="claude", no_restart=False,
                start_watcher=lambda inject_fn: None,
            )

        self.assertGreaterEqual(len(counter.read_text()), 3)
        self.assertIn(3, sleeps)  # the restart backoff

    def test_pid_holder_tracks_the_agent_process(self):
        seen = []
        pid_holder = [None]
        command = self._script("sleep 0.3\n")

        def watch(inject_fn):
            def poll():
                for _ in range(40):
                    time.sleep(0.05)
                    if pid_holder[0] is not None:
                        seen.append(pid_holder[0])
                        return
            threading.Thread(target=poll, daemon=True).start()

        wrapper_unix.run_agent(
            command=command, extra_args=[], cwd=".", env={"PATH": os.environ["PATH"]},
            queue_file=Path("queue.jsonl"), agent="claude", no_restart=True,
            start_watcher=watch, pid_holder=pid_holder,
        )

        self.assertTrue(seen, "pid_holder was never populated")
        self.assertIsNone(pid_holder[0], "pid_holder should clear once the agent exits")

    def test_missing_command_does_not_hang_the_wrapper(self):
        wrapper_unix.run_agent(
            command="/nonexistent/agent-binary", extra_args=[], cwd=".",
            env={"PATH": os.environ["PATH"]}, queue_file=Path("queue.jsonl"),
            agent="claude", no_restart=False,
            start_watcher=lambda inject_fn: None,
        )


@unittest.skipUnless(sys.platform == "win32", "Windows-only wrapper compatibility test")
class WrapperWindowsCompatibilityTests(unittest.TestCase):
    def test_run_agent_accepts_session_name_kwarg(self):
        fake_proc = mock.Mock()
        fake_proc.pid = 1234
        fake_proc.returncode = 0
        fake_proc.wait.return_value = 0

        with mock.patch.object(wrapper_windows.subprocess, "Popen", return_value=fake_proc):
            wrapper_windows.run_agent(
                command="codex",
                extra_args=[],
                cwd=".",
                env={},
                queue_file=Path("queue.jsonl"),
                agent="codex",
                no_restart=True,
                start_watcher=lambda inject_fn: None,
                session_name=None,
            )


class ProxyHeaderForwardingTests(unittest.TestCase):
    def test_post_forwards_session_and_content_type_headers_case_insensitively(self):
        seen = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                seen["authorization"] = self.headers.get("Authorization")
                seen["agent_token"] = self.headers.get("X-Agent-Token")
                body = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\r\n\r\n'
                self.send_response(200)
                # Match the lowercase header names emitted by the upstream FastMCP app.
                self.send_header("content-type", "text/event-stream")
                self.send_header("mcp-session-id", "session-123")
                self.send_header("cache-control", "no-cache, no-transform")
                self.end_headers()
                self.wfile.write(body)

        upstream = HTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        proxy = mcp_proxy.McpIdentityProxy(
            upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}",
            upstream_path="/mcp",
            agent_name="claude-7",
            instance_token="token-123",
        )
        self.assertTrue(proxy.start())

        try:
            req = Request(
                f"{proxy.url}/mcp",
                data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")
                self.assertEqual(resp.headers.get("Mcp-Session-Id"), "session-123")
                self.assertEqual(resp.headers.get("Cache-Control"), "no-cache, no-transform")
                self.assertIn('"ok":true', body)

            self.assertEqual(seen["authorization"], "Bearer token-123")
            self.assertEqual(seen["agent_token"], "token-123")
        finally:
            proxy.stop()
            upstream.shutdown()
            upstream.server_close()

    def test_get_forwards_upstream_http_errors_instead_of_hanging(self):
        seen = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                seen["authorization"] = self.headers.get("Authorization")
                seen["agent_token"] = self.headers.get("X-Agent-Token")
                body = b'{"error":"missing discovery metadata"}'
                self.send_response(404)
                self.send_header("content-type", "application/json")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

        upstream = HTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        proxy = mcp_proxy.McpIdentityProxy(
            upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}",
            upstream_path="/mcp",
            agent_name="claude",
            instance_token="token-456",
        )
        self.assertTrue(proxy.start())

        try:
            req = Request(
                f"{proxy.url}/.well-known/oauth-authorization-server",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with self.assertRaises(HTTPError) as cm:
                urlopen(req, timeout=10)

            err = cm.exception
            self.assertEqual(getattr(err, "code", None), 404)
            self.assertEqual(err.headers.get("Content-Type"), "application/json")
            self.assertEqual(err.headers.get("Cache-Control"), "no-cache")
            self.assertIn("missing discovery metadata", err.read().decode("utf-8"))
            self.assertEqual(seen["authorization"], "Bearer token-456")
            self.assertEqual(seen["agent_token"], "token-456")
        finally:
            proxy.stop()
            upstream.shutdown()
            upstream.server_close()


class ProxyDisconnectNoiseTests(unittest.TestCase):
    def test_benign_disconnects_are_suppressed(self):
        self.assertTrue(mcp_proxy._is_benign_client_disconnect(BrokenPipeError()))
        self.assertTrue(mcp_proxy._is_benign_client_disconnect(ConnectionResetError()))
        generic_os_error = OSError()
        self.assertFalse(mcp_proxy._is_benign_client_disconnect(generic_os_error))
        win_disconnect = OSError()
        win_disconnect.winerror = 10054
        self.assertTrue(
            mcp_proxy._is_benign_client_disconnect(win_disconnect)
        )
        self.assertFalse(mcp_proxy._is_benign_client_disconnect(ValueError()))


class AgentLastChannelRenameTests(unittest.TestCase):
    """Disconnect messages route to each agent's own last-active channel; that
    mapping must survive renames or the leave lands in the wrong channel."""

    def setUp(self):
        self._saved = dict(app._agent_last_channel)
        app._agent_last_channel.clear()

    def tearDown(self):
        app._agent_last_channel.clear()
        app._agent_last_channel.update(self._saved)

    def test_migrate_rekeys_old_to_new(self):
        app._agent_last_channel["claude"] = "bugfixing"
        app._migrate_agent_last_channel("claude", "claude-1")
        # New name inherits the channel; a later disconnect for "claude-1"
        # now resolves to #bugfixing instead of the global fallback.
        self.assertEqual(app._agent_last_channel.get("claude-1"), "bugfixing")
        self.assertNotIn("claude", app._agent_last_channel)

    def test_migrate_noop_when_no_prior_channel(self):
        # Renaming an agent that never spoke must not fabricate an entry.
        app._migrate_agent_last_channel("ghost", "ghost-1")
        self.assertNotIn("ghost", app._agent_last_channel)
        self.assertNotIn("ghost-1", app._agent_last_channel)

    def test_migrate_same_name_is_noop(self):
        app._agent_last_channel["codex"] = "general"
        app._migrate_agent_last_channel("codex", "codex")
        self.assertEqual(app._agent_last_channel.get("codex"), "general")

    def test_renamed_back_preserves_channel(self):
        # e.g. "claude-1" -> "claude" when the other instance leaves.
        app._agent_last_channel["claude-1"] = "design"
        app._migrate_agent_last_channel("claude-1", "claude")
        self.assertEqual(app._agent_last_channel.get("claude"), "design")
        self.assertNotIn("claude-1", app._agent_last_channel)


if __name__ == "__main__":
    unittest.main()
