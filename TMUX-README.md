# Managing agents with tmux (Mac / Linux)

On Mac and Linux, every agent CLI runs inside a **tmux** session — that's how the
wrapper injects chat prompts into the agent's terminal. This guide covers the
day-to-day tmux operations: finding sessions, connecting, diagnosing a stuck
agent, completing login prompts, restarting, and shutting agents down.

> Windows does not use tmux — agents run in their own console windows, so just
> click the agent's terminal window instead of attaching.

## Session names

Each agent gets a session named `agentchattr-<agent-name>`:

| Situation | Agent name | tmux session |
|---|---|---|
| Single instance | `claude` | `agentchattr-claude` |
| Multiple instances of the same CLI | `claude-1`, `claude-2` | `agentchattr-claude-1`, `agentchattr-claude-2` |
| Per-project isolation (`--session-prefix myproj`) | `claude` | `myproj-claude` |

The wrapper prints the exact session name at startup
(`Using tmux session: ...` / `Reattach: tmux attach -t ...`), and login alerts
in the chat UI include the attach command for the affected agent.

## Quick reference

```bash
tmux ls                                  # list all sessions (see what's running)
tmux attach -t agentchattr-claude        # connect to an agent's terminal
# Ctrl+B, then D                         # detach (agent keeps running)
tmux capture-pane -t agentchattr-claude -p   # print the agent's screen without attaching
tmux kill-session -t agentchattr-claude  # kill the agent (wrapper restarts it in 3s)
```

All tmux shortcuts start with the prefix `Ctrl+B`: press `Ctrl+B`, release,
then press the key.

## Connecting to an agent

```bash
tmux attach -t agentchattr-claude
```

You now see the agent's full TUI and can type into it directly — exactly as if
you had launched it yourself. This is how you:

- complete a **login / authentication prompt** (see below)
- answer an interactive question the CLI is stuck on (trust prompts, update
  prompts, permission confirmations)
- manually prompt the agent (e.g. type `mcp read #general` to make it check chat)

Attach from as many terminals as you like — they all mirror the same session.
For a look-don't-touch view, attach read-only:

```bash
tmux attach -t agentchattr-claude -r
```

If the panes look squashed, it's because tmux sizes a session to its smallest
attached client — detach the smaller terminal.

## Disconnecting (without stopping the agent)

Press `Ctrl+B`, then `D` to **detach**. The agent keeps running in the
background; the wrapper keeps monitoring it, heartbeats keep flowing, and
@mentions keep triggering it. Reattach any time.

Do **not** exit the shell with `Ctrl+D` or close the CLI with `Ctrl+C` /
`/exit` unless you actually want the agent process to end — that kills the
program inside the session, and the wrapper will restart it (see below).

## Diagnosing a stuck or silent agent

**1. Is the session alive?**

```bash
tmux ls
```

No session named `agentchattr-<agent>` → the agent isn't running; the wrapper
either exited or is mid-restart. Check the terminal where you launched
`start_<agent>.sh`.

**2. What's on the agent's screen right now?**

```bash
tmux capture-pane -t agentchattr-claude -p
```

This prints the visible terminal content without attaching — ideal for a quick
peek from a script or another shell. Common things you'll find:

- a **login prompt** ("Select login method", "Please run /login", "Sign in
  with...") — the agent can't respond until a human completes it. The wrapper
  also detects these and posts a ⚠️ system message in the chat UI.
- an **interactive confirmation** (trust this folder? apply this edit?) —
  attach and answer it.
- an **error message** (rate limit, network failure, crash output).
- a normal idle prompt — the agent is fine; the problem is elsewhere
  (check the server, or re-mention the agent).

To capture more than the visible screen, include scrollback lines:

```bash
tmux capture-pane -t agentchattr-claude -p -S -1000   # last 1000 lines
tmux capture-pane -t agentchattr-claude -p -S -1000 > claude-screen.log
```

**3. Scroll back while attached**

Press `Ctrl+B`, then `[` to enter scroll mode (arrow keys / PgUp to move,
`q` to leave). Useful for reading errors that have scrolled off screen.

**4. Inject input without attaching**

```bash
tmux send-keys -t agentchattr-claude -l 'mcp read #general'
tmux send-keys -t agentchattr-claude Enter
```

This is exactly what the wrapper does on @mentions — handy for testing whether
injection works at all.

## Handling login prompts

When an agent's CLI lands on a login/auth screen (expired session, first
launch, revoked token), the wrapper posts a system message in the chat UI
naming the agent and the attach command. To resolve it:

```bash
tmux attach -t agentchattr-claude   # use the session named in the alert
# complete the login flow in the TUI (pick method, open browser, paste code...)
# Ctrl+B, D to detach when done
```

Once the prompt clears, an all-clear message is posted in chat and the agent
resumes responding to mentions automatically — no restart needed.

## Restarting an agent

The wrapper auto-restarts the agent whenever its tmux session dies (unless it
was launched with `--no-restart`). So to force a clean restart:

```bash
tmux kill-session -t agentchattr-claude
```

The wrapper notices within a second and relaunches the agent in a fresh
session (same name) after ~3s. Identity, MCP config, and chat registration are
preserved — the agent rejoins as itself.

Alternatives while attached: quit the CLI its own way (`/exit`, `Ctrl+C`,
`Ctrl+D`) — same effect, the wrapper restarts it.

## Stopping an agent for good

Order matters: the wrapper is the thing that restarts sessions, so stop the
wrapper first (or at the same time), otherwise it will resurrect the agent.

- **Wrapper running in a terminal you can reach** — go to that terminal (the
  one running `start_<agent>.sh` / `wrapper.py`) and press `Ctrl+C`. This kills
  the tmux session and deregisters the agent from chat cleanly.
- **Wrapper detached/inaccessible** — kill the wrapper process, then the session:

  ```bash
  pkill -f 'wrapper.py claude'
  tmux kill-session -t agentchattr-claude
  ```

If you only `tmux kill-session` while the wrapper is still alive, the agent
comes back in 3 seconds — that's the restart feature, not a bug.

**Nuclear option:** `tmux kill-server` destroys *all* tmux sessions — every
agent, and any unrelated tmux work you have. Prefer per-session kills.

## Gotchas

- **"sessions should be nested with care"** — you're trying to attach from
  *inside* another tmux session. Either detach first, or force it with
  `TMUX= tmux attach -t agentchattr-claude`.
- **"can't find session"** — check `tmux ls` for the real name: multi-instance
  agents are numbered (`agentchattr-claude-2`), and per-project setups use a
  custom prefix instead of `agentchattr`.
- **Renamed agents** — if an agent was renamed in the chat UI, the tmux session
  keeps the name it was created with. `tmux ls` is the source of truth.
- **Headless wrappers** (launched with `--no-attach`, e.g. by the `agentchattr`
  CLI) never attach by themselves — attaching manually with `tmux attach` is
  the intended way to look at those agents.
