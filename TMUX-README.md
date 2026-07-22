# Managing agents with tmux (Mac / Linux)

On Mac and Linux the wrapper runs the agent CLI **directly, on a pseudo-terminal
it owns** — the agent is a child process of `wrapper.py`, not a separate tmux
session. Whatever terminal the wrapper is running in *is* the agent's terminal.

tmux still matters, because it's what gives you a terminal you can walk away
from and come back to. The `agentchattr` CLI puts each wrapper in its own tmux
session for exactly that reason. This guide covers the day-to-day operations:
finding sessions, connecting, diagnosing a stuck agent, completing login
prompts, restarting, and shutting agents down.

> Windows does not use tmux — agents run in their own console windows, so just
> click the agent's terminal window instead of attaching.

## Session names

The `agentchattr` CLI creates one tmux session per agent, holding the wrapper
and its agent together:

| What | tmux session |
|---|---|
| Agent #1 (`agentchattr up claude`) | `agentchattr-<project-slug>-w1-claude` |
| Agent #2 | `agentchattr-<project-slug>-w2-codex` |
| The chat server | `agentchattr-<project-slug>-server` |

You rarely type those names. Use the CLI:

```bash
agentchattr status          # what's running, with the attach command for each
agentchattr attach w1-claude
```

Login and quota alerts in the chat UI include the exact attach command for the
affected agent.

If you launched an agent the classic way (`macos-linux/start_claude.sh`, or
`python wrapper.py claude`) then the agent is in *that* terminal and there's no
tmux session at all — see [Classic launchers](#classic-launchers) below.

## Quick reference

```bash
agentchattr status                          # list agents + attach commands
agentchattr attach w1-claude                # connect to an agent's terminal
# Ctrl+B, then D                            # detach (agent keeps running)
agentchattr down codex agy2                 # stop selected configured agents
agentchattr up codex agy2                   # restart them from current config
agentchattr down                            # stop everything for this project

tmux ls                                     # raw session list, all projects
tmux capture-pane -t agentchattr-myproj-w1-claude -p   # peek without attaching
```

All tmux shortcuts start with the prefix `Ctrl+B`: press `Ctrl+B`, release,
then press the key.

## Connecting to an agent

```bash
agentchattr attach w1-claude
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
tmux attach -t agentchattr-myproj-w1-claude -r
```

If the panes look squashed, it's because tmux sizes a session to its smallest
attached client — detach the smaller terminal. The wrapper follows the size
change and resizes the agent's terminal to match.

## Disconnecting (without stopping the agent)

Press `Ctrl+B`, then `D` to **detach**. The wrapper and agent keep running in
the background, heartbeats keep flowing, and @mentions keep triggering it.
Reattach any time.

Do **not** exit the CLI with `/exit` or `Ctrl+C` unless you actually want the
agent process to end — the wrapper will restart it (see below). `Ctrl+C` goes
to the agent, not the wrapper: the wrapper puts your terminal in raw mode so
the agent receives keys exactly as you typed them.

## Diagnosing a stuck or silent agent

**1. Is it running?**

```bash
agentchattr status
```

A missing agent means the wrapper exited. Check its log:

```bash
agentchattr logs w1-claude
```

**2. What's on the agent's screen right now?**

```bash
tmux capture-pane -t agentchattr-myproj-w1-claude -p
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
tmux capture-pane -t agentchattr-myproj-w1-claude -p -S -1000
```

The wrapper reads the agent's screen the same way internally, but it doesn't
shell out to tmux to do it — it renders the agent's own terminal output through
a terminal emulator, so login and quota detection work identically whether or
not tmux is in the picture.

**3. Scroll back while attached**

Press `Ctrl+B`, then `[` to enter scroll mode (arrow keys / PgUp to move,
`q` to leave). Useful for reading errors that have scrolled off screen.

**4. Inject input without attaching**

```bash
tmux send-keys -t agentchattr-myproj-w1-claude -l 'mcp read #general'
tmux send-keys -t agentchattr-myproj-w1-claude Enter
```

The wrapper doesn't use `send-keys` for @mentions any more — it writes straight
to the agent's terminal — but this still works for testing, because tmux is
delivering the keys to the same terminal the wrapper is attached to.

## Handling login prompts

When an agent's CLI lands on a login/auth screen (expired session, first
launch, revoked token), the wrapper posts a system message in the chat UI
naming the agent and the attach command. To resolve it:

```bash
agentchattr attach w1-claude   # use the command named in the alert
# complete the login flow in the TUI (pick method, open browser, paste code...)
# Ctrl+B, D to detach when done
```

Once the prompt clears, an all-clear message is posted in chat and the agent
resumes responding to mentions automatically — no restart needed.

## Restarting an agent

The wrapper auto-restarts the agent whenever it exits (unless it was launched
with `--no-restart`). So to force a clean restart, quit the CLI its own way
while attached (`/exit`, `Ctrl+C`, `Ctrl+D`), or kill the agent process:

```bash
pkill -f 'claude'    # match the agent CLI, not wrapper.py
```

The wrapper notices immediately and relaunches the agent after ~3s. Identity,
MCP config, and chat registration are preserved — the agent rejoins as itself.

Killing the tmux session no longer restarts the agent — it takes the wrapper
down with it, because they now live in the same session.

## Stopping an agent for good

```bash
agentchattr down codex    # selected agent only; server and others stay running
agentchattr down          # this project: agents, wrappers, server
agentchattr down --purge  # also delete the project's instance dir
```

That stops wrappers first, so nothing gets resurrected, and deregisters the
agents from chat cleanly.

To stop a single agent, kill its session — the wrapper goes down with it and
won't restart anything:

```bash
tmux kill-session -t agentchattr-myproj-w1-claude
```

**Nuclear option:** `tmux kill-server` destroys *all* tmux sessions — every
agent, every project, and any unrelated tmux work you have. Prefer
`agentchattr down`.

## Classic launchers

`macos-linux/start_<agent>.sh` and `python wrapper.py <agent>` run the wrapper
in your current terminal, and the agent now runs inside it. That means:

- you see the agent's TUI immediately, no attach step
- **closing the terminal stops the agent** — there's no background session to
  detach from

If you want to be able to walk away, start it inside tmux yourself:

```bash
tmux new -s claude -c /path/to/agentchattr './macos-linux/start_claude.sh'
# Ctrl+B, D to detach; tmux attach -t claude to come back
```

Or just use `agentchattr up`, which does this for you.

## Gotchas

- **"sessions should be nested with care"** — you're trying to attach from
  *inside* another tmux session. Either detach first, or force it with
  `TMUX= agentchattr attach w1-claude`.
- **"No session matching ..."** — run `agentchattr status` for the real names.
  Sessions are per-project: run it from the project directory you started the
  agents in.
- **Renamed agents** — if an agent was renamed in the chat UI, its session keeps
  the name it was created with. `agentchattr status` is the source of truth.
- **Stray `agentchattr-<slug>-<agent>` sessions** — left over from a version
  that ran the agent in its own nested session. `agentchattr down` clears them.
