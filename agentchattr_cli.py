"""agentchattr CLI — launch per-project isolated agent swarms from any directory.

Installed as the global `agentchattr` command (see install.sh). Run from a
project directory:

    cd ~/projects/myapp
    agentchattr up claude codex agy agy   # server + agents, all detached
    agentchattr status                    # what's running here
    agentchattr attach agy-2              # watch an agent (Ctrl+B, D detaches)
    agentchattr logs w1-claude            # tail a wrapper's log
    agentchattr ui                        # open this project's chat UI
    agentchattr down codex agy2           # stop selected agents only
    agentchattr down                      # stop this project's whole swarm

Each project gets a fully isolated instance: its own server/MCP ports
(allocated deterministically from the project path), data dir, uploads,
and logs — all under ~/.agentchattr/instances/<slug>/. Every process runs
in a detached tmux session named agentchattr-<slug>-*, so nothing is tied
to the launching terminal:

    agentchattr-<slug>-server        run.py (web UI + MCP servers)
    agentchattr-<slug>-w<N>-<base>   wrapper.py controller for agent N
    agentchattr-<slug>-<name>        the agent CLI itself (created by wrapper)

The classic single-instance launchers (macos-linux/start_*.sh, port 8300)
keep working unchanged; this CLI allocates ports from 8310 up.
"""

import argparse
import hashlib
import json
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

INSTANCES_DIR = Path.home() / ".agentchattr" / "instances"

# Port allocation: deterministic per project path, colliding neither with the
# stock ports (8200/8201/8300) nor with other instances.
PORT_BASE = 8310
PORT_SLOTS = 560
PORT_STRIDE = 10  # server, mcp_http, mcp_sse per slot; room to grow

# Imports from a virtualenv on a WSL-mounted drive can take close to a minute
# on a cold filesystem cache (FastAPI/Pydantic are the bulk of it). Give a
# healthy server enough time while `_server_ready` still exits early if its
# tmux process dies.
SERVER_READY_TIMEOUT = 120.0


def _python_bin() -> str:
    venv_py = ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


# ---------------------------------------------------------------------------
# Project identity & state
# ---------------------------------------------------------------------------

def slug_for(project_dir: Path) -> str:
    """Stable, human-readable, collision-free id for a project path."""
    base = re.sub(r"[^a-z0-9]+", "-", project_dir.name.lower()).strip("-") or "project"
    digest = hashlib.sha1(str(project_dir).encode()).hexdigest()[:8]
    return f"{base}-{digest}"


def instance_dir(slug: str) -> Path:
    return INSTANCES_DIR / slug


def state_path(slug: str) -> Path:
    return instance_dir(slug) / "instance.json"


def load_state(slug: str) -> dict | None:
    path = state_path(slug)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


def save_state(slug: str, state: dict) -> None:
    path = state_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", "utf-8")


def _all_states() -> list[dict]:
    states = []
    if INSTANCES_DIR.exists():
        for path in sorted(INSTANCES_DIR.glob("*/instance.json")):
            try:
                states.append(json.loads(path.read_text("utf-8")))
            except Exception:
                pass
    return states


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def _port_bindable(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def allocate_ports(project_dir: Path, slug: str) -> dict:
    """Deterministic port trio for the project, stepping past collisions."""
    claimed: set[int] = set()
    for other in _all_states():
        if other.get("slug") == slug:
            continue
        claimed.update(other.get("ports", {}).values())

    i = zlib.crc32(str(project_dir).encode()) % PORT_SLOTS
    for _ in range(PORT_SLOTS):
        base = PORT_BASE + i * PORT_STRIDE
        trio = {"server": base, "mcp_http": base + 1, "mcp_sse": base + 2}
        ports = trio.values()
        if not claimed.intersection(ports) and all(_port_bindable(p) for p in ports):
            return trio
        i = (i + 1) % PORT_SLOTS
    print("Error: no free port slot found (checked all candidates).")
    sys.exit(1)


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------

def _check_tmux() -> None:
    import shutil
    if not shutil.which("tmux"):
        print("Error: tmux is required. Install it with: sudo apt install tmux")
        sys.exit(1)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def tmux_sessions(prefix: str) -> list[str]:
    result = _tmux("list-sessions", "-F", "#S")
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.splitlines() if s.startswith(prefix + "-")]


def session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", f"={name}").returncode == 0


def kill_session(name: str) -> None:
    _tmux("kill-session", "-t", f"={name}")


def new_session(name: str, cwd: Path, cmd: list[str], logfile: Path | None = None) -> bool:
    """Start `cmd` in a detached tmux session, optionally piping output to a log."""
    result = _tmux("new-session", "-d", "-s", name, "-c", str(cwd),
                   " ".join(shlex.quote(a) for a in cmd))
    if result.returncode != 0:
        print(f"Error: failed to start tmux session {name}: {result.stderr.strip()}")
        return False
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        _tmux("pipe-pane", "-o", "-t", name, f"cat >> {shlex.quote(str(logfile))}")
    return True


# ---------------------------------------------------------------------------
# Instance context
# ---------------------------------------------------------------------------

class Instance:
    """Everything derived from the invoking directory."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.slug = slug_for(project_dir)
        self.prefix = f"agentchattr-{self.slug}"
        self.dir = instance_dir(self.slug)
        self.data_dir = self.dir / "data"
        self.upload_dir = self.dir / "uploads"
        self.logs_dir = self.dir / "logs"

    @classmethod
    def here(cls) -> "Instance":
        cwd = Path.cwd().resolve()
        if cwd == ROOT or ROOT in cwd.parents:
            print("Error: you're inside the agentchattr install directory.")
            print("Run this from a project directory (or use macos-linux/start_*.sh")
            print("for the classic single-instance setup).")
            sys.exit(1)
        return cls(cwd)

    def state(self) -> dict | None:
        return load_state(self.slug)

    def wrapper_sessions(self) -> list[tuple[int, str, str]]:
        """Live wrapper controller sessions as (N, base, session_name)."""
        out = []
        pattern = re.compile(rf"^{re.escape(self.prefix)}-w(\d+)-(.+)$")
        for name in tmux_sessions(self.prefix):
            m = pattern.match(name)
            if m:
                out.append((int(m.group(1)), m.group(2), name))
        return sorted(out)

    def agent_tui_sessions(self) -> list[str]:
        """Stray agent CLI sessions (everything that's not server or a controller).

        Wrappers used to start a separate session for the agent; they now run
        it on their own PTY, so this is only ever non-empty for sessions left
        behind by a wrapper from before that change.
        """
        out = []
        for name in tmux_sessions(self.prefix):
            rest = name[len(self.prefix) + 1:]
            if rest == "server" or re.match(r"^w\d+-", rest):
                continue
            out.append(name)
        return sorted(out)

    def server_session(self) -> str:
        return f"{self.prefix}-server"


def _load_agents_config(project_dir: Path | None = None) -> dict:
    from config_loader import load_config
    return load_config(ROOT, project_dir=project_dir).get("agents", {})


def parse_agent_spec(spec: str) -> tuple[str, str | None]:
    """Split an `up` argument into (agent, mode). `codex:deep` -> ("codex", "deep")."""
    base, sep, mode = spec.partition(":")
    return base, (mode if sep and mode else None)


def select_wrapper_sessions(
    wrappers: list[tuple[int, str, str]],
    targets: list[str],
    aliases: dict[str, str] | None = None,
) -> tuple[list[tuple[int, str, str]], list[str]]:
    """Resolve selective-down targets to live wrapper sessions.

    A config agent name (``agy``) selects every running instance of that
    agent.  A status name (``w2-agy``) selects exactly one instance.
    """
    selected: list[tuple[int, str, str]] = []
    missing: list[str] = []
    seen_sessions: set[str] = set()
    aliases = aliases or {}

    for target in targets:
        resolved_target = aliases.get(target, target)
        matches = [
            wrapper for wrapper in wrappers
            if resolved_target == wrapper[1]
            or resolved_target == f"w{wrapper[0]}-{wrapper[1]}"
        ]
        if not matches:
            missing.append(target)
            continue
        for wrapper in matches:
            if wrapper[2] not in seen_sessions:
                selected.append(wrapper)
                seen_sessions.add(wrapper[2])

    return selected, missing


def _server_ready(port: int, timeout: float, alive_checker=None) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        if alive_checker is not None and not alive_checker():
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _tail(path: Path, n: int = 20) -> str:
    try:
        return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return "(no log)"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_up(args) -> int:
    _check_tmux()
    inst = Instance.here()
    agents_cfg = _load_agents_config(inst.project_dir)

    if not args.agents:
        print("Usage: agentchattr up <agent>[:<mode>] ...   e.g. agentchattr up claude:yolo codex agy agy")
        print(f"Available agents: {', '.join(sorted(agents_cfg))}")
        return 1

    specs = [parse_agent_spec(s) for s in args.agents]
    for base, mode in specs:
        cfg = agents_cfg.get(base)
        if cfg is None:
            print(f"Error: unknown agent '{base}'. Available: {', '.join(sorted(agents_cfg))}")
            return 1
        if cfg.get("type") == "api":
            print(f"Error: '{base}' is an API agent — not supported by `up` yet.")
            print(f"Run it manually instead: python wrapper_api.py {base}")
            return 1
        if mode and mode not in cfg.get("modes", {}):
            known = ", ".join(sorted(cfg.get("modes", {}))) or "none defined"
            print(f"Error: unknown mode '{mode}' for agent '{base}' (available: {known})")
            return 1

    state = inst.state() or {
        "version": 1,
        "project_dir": str(inst.project_dir),
        "slug": inst.slug,
        "server_name": inst.prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ports = state.get("ports")
    server_up = bool(ports) and session_exists(inst.server_session()) \
        and _server_ready(ports["server"], timeout=2)
    if not server_up:
        if not ports:
            ports = allocate_ports(inst.project_dir, inst.slug)
            state["ports"] = ports
        elif not all(_port_bindable(p) or _port_listening(p) for p in ports.values()):
            # Persisted ports stolen by something else — reallocate.
            ports = allocate_ports(inst.project_dir, inst.slug)
            state["ports"] = ports

    for d in (inst.data_dir, inst.upload_dir, inst.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    py = _python_bin()
    iso_flags = [
        "--port", str(ports["server"]),
        "--mcp-http-port", str(ports["mcp_http"]),
        "--mcp-sse-port", str(ports["mcp_sse"]),
        "--data-dir", str(inst.data_dir),
        "--upload-dir", str(inst.upload_dir),
        # The server needs the project dir too, not just the wrappers: it reads
        # the project's agentchattr.toml to seed its registry, and an agent
        # missing from that roster rejects its wrapper's registration.
        "--cwd", str(inst.project_dir),
    ]

    # --- Server ---
    if server_up:
        print(f"Server already running on port {ports['server']}.")
    else:
        kill_session(inst.server_session())  # clear a stale/hung session if any
        print(f"Starting server on port {ports['server']} ...")
        if not new_session(inst.server_session(), ROOT,
                           [py, str(ROOT / "run.py"), *iso_flags],
                           logfile=inst.logs_dir / "server.log"):
            return 1
        if not _server_ready(
            ports["server"],
            SERVER_READY_TIMEOUT,
            alive_checker=lambda: session_exists(inst.server_session()),
        ):
            print(f"Error: server didn't become ready within {int(SERVER_READY_TIMEOUT)}s.")
            print(f"Last log lines ({inst.logs_dir / 'server.log'}):")
            print(_tail(inst.logs_dir / "server.log"))
            kill_session(inst.server_session())
            return 1

    # --- Agents (idempotent: only start the deficit per base) ---
    running = inst.wrapper_sessions()
    running_counts: dict[str, int] = {}
    for _, base, _name in running:
        running_counts[base] = running_counts.get(base, 0) + 1
    # Modes aren't part of a session name, so restarts are counted per base:
    # `up claude claude:yolo` with one claude already running starts the yolo
    # one, matching the specs in the order they were given.
    requested: dict[str, list[str | None]] = {}
    for base, mode in specs:
        requested.setdefault(base, []).append(mode)

    used_ns = {n for n, _, _ in running}
    next_n = 1
    started = []
    for base, modes_wanted in requested.items():
        have = running_counts.get(base, 0)
        for mode in modes_wanted[have:]:
            while next_n in used_ns:
                next_n += 1
            used_ns.add(next_n)
            name = f"{inst.prefix}-w{next_n}-{base}"
            cmd = [
                py, str(ROOT / "wrapper.py"),
                "--no-attach",
                "--replace-reclaimable",
                *iso_flags,
                "--session-prefix", inst.prefix,
                "--mcp-server-name", inst.prefix,
            ]
            if mode:
                cmd += ["--mode", mode]
            if args.model:
                cmd += ["--model", args.model]
            if args.effort:
                cmd += ["--effort", args.effort]
            cmd.append(base)
            if not new_session(name, ROOT, cmd, logfile=inst.logs_dir / f"w{next_n}-{base}.log"):
                return 1
            started.append((next_n, base, mode))

    state["agents"] = sorted({base for _, base, _ in inst.wrapper_sessions()}
                             | {b for _, b, _ in started}
                             | set(state.get("agents", [])))
    state["last_up"] = datetime.now(timezone.utc).isoformat()
    save_state(inst.slug, state)

    if started:
        print(f"Started {len(started)} agent(s): " +
              ", ".join(f"w{n}-{b}" + (f" ({m})" if m else "") for n, b, m in started))
    else:
        print("All requested agents already running.")
    already = sum(min(running_counts.get(b, 0), len(m)) for b, m in requested.items())
    if already:
        print(f"({already} already running)")
    print()
    print(f"Project:  {inst.project_dir}")
    print(f"Instance: {inst.slug}")
    print(f"UI:       http://127.0.0.1:{ports['server']}/")
    print(f"Logs:     {inst.logs_dir}")
    print()
    print("Everything runs detached — closing this terminal stops nothing.")
    print("Watch an agent:  agentchattr status && agentchattr attach <name>")
    print("Stop the swarm:  agentchattr down")
    return 0


def cmd_status(args) -> int:
    _check_tmux()
    if args.all:
        states = _all_states()
        if not states:
            print("No instances found.")
            return 0
        code = 0
        for st in states:
            proj = Path(st.get("project_dir", "?"))
            print(f"=== {st.get('slug', '?')}  ({proj})")
            _print_instance_status(Instance(proj))
            print()
        return code
    inst = Instance.here()
    if inst.state() is None and not tmux_sessions(inst.prefix):
        print(f"No instance for {inst.project_dir}.")
        print("Start one with: agentchattr up <agent> [<agent> ...]")
        return 0
    _print_instance_status(inst)
    return 0


def _print_instance_status(inst: Instance) -> None:
    state = inst.state() or {}
    ports = state.get("ports", {})
    server_port = ports.get("server")
    server_session_up = session_exists(inst.server_session())
    server_http_up = bool(server_port) and _port_listening(server_port)

    wrappers = inst.wrapper_sessions()
    tuis = inst.agent_tui_sessions()

    if server_session_up and server_http_up:
        print(f"server     running    http://127.0.0.1:{server_port}/")
    elif server_session_up:
        print(f"server     starting?  session up, port {server_port} not answering "
              f"(see {inst.logs_dir / 'server.log'})")
    elif wrappers or tuis:
        print("server     DOWN       (agents still running — re-run 'agentchattr up ...' to restart it)")
    else:
        print("server     stopped")

    if not wrappers and not tuis:
        if state:
            print("agents     none running (stale state — 'agentchattr up ...' to restart, "
                  "'agentchattr down --purge' to remove)")
        return

    # One session per agent: the wrapper runs the agent CLI on its own PTY,
    # so the controller session *is* where the agent's terminal lives.
    for n, base, _name in wrappers:
        label = f"w{n}-{base}"
        print(f"{label:<10} running    attach: agentchattr attach {label}"
              f"   log: {inst.logs_dir / f'{label}.log'}")
    for t in tuis:
        print(f"{t[len(inst.prefix) + 1:]:<10} stray agent session from an older version "
              f"('agentchattr down' clears it)")


def cmd_down(args) -> int:
    _check_tmux()
    inst = Instance.here()
    state = inst.state() or {}

    # With positional targets, stop only those wrappers.  The server and MCP
    # settings stay alive so the remaining swarm is unaffected; `up` can then
    # recreate the stopped agents from the latest project config.
    if args.agents:
        if args.purge:
            print("Error: --purge cannot be used when stopping selected agents.")
            return 1
        wrappers = inst.wrapper_sessions()
        from registry import normalize_agent_handle

        agents_cfg = _load_agents_config(inst.project_dir)
        aliases = {
            normalize_agent_handle(cfg.get("label", "")): base
            for base, cfg in agents_cfg.items()
            if normalize_agent_handle(cfg.get("label", ""))
        }
        selected, missing = select_wrapper_sessions(wrappers, args.agents, aliases)
        if missing:
            running = [f"w{n}-{base}" for n, base, _ in wrappers]
            print("Error: no running agent matched: " + ", ".join(missing))
            if running:
                print("Running agents: " + ", ".join(running))
            else:
                print("No agents are running for this project.")
            return 1

        for _, _, name in selected:
            kill_session(name)

        stopped = [f"w{n}-{base}" for n, base, _ in selected]
        print(f"Stopped {len(stopped)} agent(s): " + ", ".join(stopped))
        print("Server and other agents are still running.")
        restart_agents = " ".join(base for _, base, _ in selected)
        print("Start them with the latest config: agentchattr up " + restart_agents)
        if state:
            state["last_partial_down"] = datetime.now(timezone.utc).isoformat()
            save_state(inst.slug, state)
        return 0

    # 1. Controllers first — stops restart loops so agents can't resurrect.
    wrappers = inst.wrapper_sessions()
    for _, _, name in wrappers:
        kill_session(name)
    # 2. Stray agent sessions left by a pre-PTY wrapper.
    tuis = inst.agent_tui_sessions()
    for name in tuis:
        kill_session(name)
    # 3. Server.
    had_server = session_exists(inst.server_session())
    kill_session(inst.server_session())
    server_port = state.get("ports", {}).get("server")
    if server_port:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _port_listening(server_port):
            time.sleep(0.1)

    # 4. Best-effort: remove this instance's entry from shared per-user MCP
    #    settings files (agy/copilot/codebuddy style settings_file injection).
    server_name = state.get("server_name", inst.prefix)
    agents_cfg = _load_agents_config(inst.project_dir)
    cleaned = []
    for base in state.get("agents", []):
        cfg = agents_cfg.get(base, {})
        raw = cfg.get("mcp_settings_path", "")
        if cfg.get("mcp_inject") != "settings_file" or not raw.startswith("~"):
            continue
        path = Path(raw).expanduser()
        try:
            data = json.loads(path.read_text("utf-8"))
            if server_name in data.get("mcpServers", {}):
                del data["mcpServers"][server_name]
                path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
                cleaned.append(str(path))
        except Exception:
            pass

    killed = len(wrappers) + len(tuis) + (1 if had_server else 0)
    if killed:
        print(f"Stopped {len(wrappers)} controller(s), {len(tuis)} agent session(s)"
              + (", server" if had_server else "") + ".")
    else:
        print("Nothing was running.")
    for path in cleaned:
        print(f"Removed MCP entry '{server_name}' from {path}")

    if args.purge:
        import shutil
        shutil.rmtree(inst.dir, ignore_errors=True)
        print(f"Purged {inst.dir}")
    elif state:
        state["last_down"] = datetime.now(timezone.utc).isoformat()
        save_state(inst.slug, state)
    return 0


def cmd_attach(args) -> int:
    _check_tmux()
    inst = Instance.here()
    target = f"{inst.prefix}-{args.name}"
    if not session_exists(target):
        matches = [s for s in tmux_sessions(inst.prefix) if args.name in s]
        if len(matches) == 1:
            target = matches[0]
        else:
            live = tmux_sessions(inst.prefix)
            if matches:
                print(f"Ambiguous name '{args.name}'. Matches: "
                      + ", ".join(m[len(inst.prefix) + 1:] for m in matches))
            elif live:
                print(f"No session matching '{args.name}'. Running: "
                      + ", ".join(s[len(inst.prefix) + 1:] for s in live))
            else:
                print("No sessions running for this project.")
            return 1
    # Replace this process — Ctrl+B, D detaches back to the shell.
    import os
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={target}"])


def cmd_ui(args) -> int:
    inst = Instance.here()
    state = inst.state()
    if not state or "ports" not in state:
        print("No instance for this project. Start one with: agentchattr up <agent> ...")
        return 1
    url = f"http://127.0.0.1:{state['ports']['server']}/"
    import webbrowser
    if not webbrowser.open(url):
        pass
    print(url)
    return 0


def cmd_logs(args) -> int:
    inst = Instance.here()
    if not inst.logs_dir.exists():
        print("No logs for this project yet.")
        return 1
    candidates = sorted(inst.logs_dir.glob("*.log"))
    match = [p for p in candidates if args.name in p.stem] if args.name else candidates
    if not match:
        print(f"No log matching '{args.name}'. Available: "
              + ", ".join(p.stem for p in candidates))
        return 1
    if len(match) > 1 and args.name:
        print("Matched: " + ", ".join(p.stem for p in match))
    try:
        subprocess.run(["tail", "-n", "50", "-F", *[str(p) for p in match]])
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentchattr",
        description="Launch per-project isolated agent swarms from any directory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="Start the server and the listed agents in this project dir")
    p_up.add_argument("agents", nargs="*",
                      help="Agent names from config.toml, optionally <agent>:<mode>; "
                           "repeat for multiple instances (e.g. agy agy claude:yolo)")
    p_up.add_argument("--model",  default=None, help="Model for every agent started by this command")
    p_up.add_argument("--effort", default=None, help="Reasoning effort for every agent started by this command")
    p_up.set_defaults(func=cmd_up)

    p_status = sub.add_parser("status", help="Show this project's swarm status")
    p_status.add_argument("--all", action="store_true", help="Show every instance on this machine")
    p_status.set_defaults(func=cmd_status)

    p_down = sub.add_parser(
        "down",
        help="Stop selected agents, or the whole swarm when none are listed",
    )
    p_down.add_argument(
        "agents", nargs="*",
        help="Config/label names (all instances) or exact status names such as w2-agy",
    )
    p_down.add_argument("--purge", action="store_true", help="Also delete the instance's state/data/logs")
    p_down.set_defaults(func=cmd_down)

    p_attach = sub.add_parser("attach", help="Attach to a session (agent name, 'server', or 'w1-claude')")
    p_attach.add_argument("name")
    p_attach.set_defaults(func=cmd_attach)

    p_ui = sub.add_parser("ui", help="Open this project's chat UI in the browser")
    p_ui.set_defaults(func=cmd_ui)

    p_logs = sub.add_parser("logs", help="Tail wrapper/server logs")
    p_logs.add_argument("name", nargs="?", default="", help="Log name filter (e.g. server, w1-claude)")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
