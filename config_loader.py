"""Shared config loader — merges config.toml + config.local.toml.

Used by run.py, wrapper.py, and wrapper_api.py so the server and all
wrappers see the same agent definitions.

Per-invocation overrides: the following environment variables, if set,
override values from config.toml. This lets dotfiles/launcher layers run
isolated instances per project without editing the repo's config file.

  AGENTCHATTR_DATA_DIR        → server.data_dir
  AGENTCHATTR_PORT            → server.port           (int)
  AGENTCHATTR_MCP_HTTP_PORT   → mcp.http_port         (int)
  AGENTCHATTR_MCP_SSE_PORT    → mcp.sse_port          (int)
  AGENTCHATTR_UPLOAD_DIR      → images.upload_dir

Overrides consumed directly by entry points rather than as config keys:

  AGENTCHATTR_CWD             → the project directory. Sourced for the
                                project-local agentchattr.toml by both the
                                server and the wrappers (see
                                resolve_project_dir), and additionally used by
                                wrapper.py as the agent's working directory,
                                overriding every agent's `cwd`.
  AGENTCHATTR_SESSION_PREFIX  → tmux session name prefix (default
                                "agentchattr"; per-project instances use a
                                unique prefix so sessions never collide)
  AGENTCHATTR_MCP_SERVER_NAME → mcpServers key used when injecting MCP
                                settings (default "agentchattr"; per-project
                                instances use a unique key so shared per-user
                                settings files aren't clobbered)

Relative paths in env var overrides resolve against the current working
directory (where the user invoked the command from), not agentchattr's
install directory.
"""

import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent


# Mapping: env var name → (config section, key, is_int)
_ENV_OVERRIDES = [
    ("AGENTCHATTR_DATA_DIR",      "server", "data_dir",   False),
    ("AGENTCHATTR_PORT",          "server", "port",       True),
    ("AGENTCHATTR_MCP_HTTP_PORT", "mcp",    "http_port",  True),
    ("AGENTCHATTR_MCP_SSE_PORT",  "mcp",    "sse_port",   True),
    ("AGENTCHATTR_UPLOAD_DIR",    "images", "upload_dir", False),
]

# Mapping: CLI flag → env var (for apply_cli_overrides)
CLI_OVERRIDE_FLAGS = [
    ("--data-dir",      "AGENTCHATTR_DATA_DIR"),
    ("--port",          "AGENTCHATTR_PORT"),
    ("--mcp-http-port", "AGENTCHATTR_MCP_HTTP_PORT"),
    ("--mcp-sse-port",  "AGENTCHATTR_MCP_SSE_PORT"),
    ("--upload-dir",    "AGENTCHATTR_UPLOAD_DIR"),
    ("--cwd",             "AGENTCHATTR_CWD"),
    ("--session-prefix",  "AGENTCHATTR_SESSION_PREFIX"),
    ("--mcp-server-name", "AGENTCHATTR_MCP_SERVER_NAME"),
]


def apply_cli_overrides(argv: list[str] | None = None) -> None:
    """Scan argv for --data-dir/--port/etc and set matching env vars in-place.

    Called by run.py, wrapper.py, and wrapper_api.py BEFORE load_config() so
    all entry points respect the same overrides when launched with the same
    flags. No effect if a flag isn't present. Supports both `--flag value`
    and `--flag=value` forms.

    Arguments after a literal `--` are treated as pass-through (e.g. for the
    agent CLI in wrapper.py) and are NOT scanned — `python wrapper.py claude
    -- --port 9999` sets `--port 9999` on the agent, not on agentchattr.
    """
    if argv is None:
        argv = sys.argv

    # Truncate at pass-through separator so agent CLI args don't leak in.
    try:
        end = argv.index("--")
        scan = argv[:end]
    except ValueError:
        scan = argv

    for flag, env in CLI_OVERRIDE_FLAGS:
        # Iterate in order; first match wins (ignore later duplicates).
        for i, arg in enumerate(scan):
            if arg == flag and i + 1 < len(scan):
                os.environ[env] = scan[i + 1]
                break
            if arg.startswith(flag + "="):
                os.environ[env] = arg.split("=", 1)[1]
                break


def resolve_project_dir() -> Path | None:
    """Absolute project dir from AGENTCHATTR_CWD, or None if unset.

    The project dir is where a project-local agentchattr.toml lives, so the
    server and every wrapper must resolve it the same way or they end up with
    different agent rosters — a wrapper launching an agent the server's
    registry has never heard of, which fails registration. Relative paths
    resolve against the invoking shell's cwd, matching the env-override rule
    for paths above.
    """
    raw = os.environ.get("AGENTCHATTR_CWD", "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else Path.cwd() / path).resolve()


def _apply_env_overrides(config: dict) -> None:
    """Apply AGENTCHATTR_* env vars to the config dict in-place."""
    for env_var, section, key, is_int in _ENV_OVERRIDES:
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        if is_int:
            try:
                value = int(raw)
            except ValueError:
                print(f"  Warning: {env_var}={raw!r} is not a valid integer, ignoring")
                continue
        else:
            # Path values: resolve relative paths against current working dir,
            # not against agentchattr's install directory.
            p = Path(raw)
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            value = str(p)
        config.setdefault(section, {})[key] = value


def resolve_launch_args(
    agent: str,
    agent_cfg: dict,
    mode: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve an agent's launch args from config + mode + per-run overrides.

    Returns (launch_args, warnings). Raises ValueError for an unknown mode.

    Precedence, lowest to highest: the agent's own keys, then the named mode's
    keys, then the `model`/`effort` arguments (which come from the wrapper's
    --model/--effort flags). Args accumulate rather than replace, so a mode's
    `args` are appended after the agent's.

    `model`/`effort` are values, not flags — each is rendered through the
    agent's `model_flag_template` / `effort_flag_template` because every CLI
    spells them differently (`--model X` vs `-m X` vs `-c key="X"`). Templates
    are split on whitespace, not shell-lexed, so quoting inside a template
    survives into the argv the agent sees (codex needs `"high"` to stay quoted
    to parse as TOML). An agent with no template for a key it was given can't
    honour it — that's a warning, not a silent drop.
    """
    warnings: list[str] = []

    modes = agent_cfg.get("modes", {})
    mode_cfg: dict = {}
    if mode:
        if mode not in modes:
            known = ", ".join(sorted(modes)) or "none defined"
            raise ValueError(f"unknown mode '{mode}' for agent '{agent}' (available: {known})")
        mode_cfg = modes[mode]

    # For type="api" agents `model` names the model in the API request body
    # (wrapper_api.py reads it directly) rather than a CLI flag, so only an
    # explicitly passed override applies here.
    is_api = agent_cfg.get("type") == "api"
    resolved_model = model or mode_cfg.get("model") or (None if is_api else agent_cfg.get("model"))
    resolved_effort = effort or mode_cfg.get("effort") or agent_cfg.get("effort")

    args: list[str] = []
    for key, value, template_key in (
        ("model", resolved_model, "model_flag_template"),
        ("effort", resolved_effort, "effort_flag_template"),
    ):
        if not value:
            continue
        template = agent_cfg.get(template_key)
        if not template:
            warnings.append(
                f"agent '{agent}' has no {template_key} — ignoring {key} = {value!r}"
            )
            continue
        args.extend(template.format(**{key: value}).split())

    args.extend(agent_cfg.get("args", []))
    args.extend(mode_cfg.get("args", []))

    return args, warnings


def _merge_agents_file(
    config_agents: dict, path: Path, label: str, allow_override: bool = False
) -> None:
    """Merge a TOML file's [agents] section into config_agents in-place.

    With allow_override=False an entry is added ONLY if its name isn't already
    present, protecting agents defined upstream from a lower-precedence file.

    With allow_override=True an entry REPLACES any existing agent of the same
    name. The replacement is whole-table, not per-key: the upstream definition
    is dropped entirely, so an overriding block must restate every key it needs
    (mcp_inject, mcp_settings_path, color, ...) or those settings are lost.
    """
    if not path.exists():
        return
    with open(path, "rb") as f:
        extra = tomllib.load(f)
    for name, agent_cfg in extra.get("agents", {}).items():
        if name not in config_agents:
            config_agents[name] = agent_cfg
        elif allow_override:
            config_agents[name] = agent_cfg
            print(f"  Agent '{name}' overridden by {label} config.")
        else:
            print(f"  Warning: Ignoring {label} agent '{name}' (already defined)")


def load_config(root: Path | None = None, project_dir: Path | None = None) -> dict:
    """Load config.toml, merging config.local.toml and a project's agentchattr.toml.

    config.local.toml lives next to config.toml (gitignored) and is intended
    for user-specific agents (e.g. local LLM endpoints) that shouldn't be
    committed to the agentchattr install.

    agentchattr.toml, if project_dir is given, lives in the project directory
    you ran `agentchattr up` from — it lets a project define extra agents
    (e.g. a second Antigravity instance "agy2" with its own default flags) or
    retune the shared ones (a different model, extra launch flags) without
    touching the install. It's read/write per-project, so it's fine to commit
    if the team wants to share it.

    Both are merged into [agents] only. config.local.toml can only ADD agents
    that config.toml doesn't already define. agentchattr.toml wins outright:
    a project block REPLACES the whole upstream table for that agent, so it
    must restate every key it needs — keys it omits are not inherited.

    Precedence, lowest to highest: config.toml, config.local.toml,
    agentchattr.toml.

    AGENTCHATTR_* environment variables override values from config.toml
    (see module docstring for the list).
    """
    root = root or ROOT
    config_path = root / "config.toml"

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    config_agents = config.setdefault("agents", {})
    _merge_agents_file(config_agents, root / "config.local.toml", "local")
    if project_dir is not None:
        _merge_agents_file(
            config_agents,
            project_dir / "agentchattr.toml",
            "project",
            allow_override=True,
        )

    _apply_env_overrides(config)

    return config
