"""Mac/Linux agent injection — runs the agent CLI on a PTY owned by the wrapper.

Called by wrapper.py on Mac and Linux. No external terminal multiplexer is
involved: the agent CLI is a direct child process of the Python wrapper,
attached to a pseudo-terminal the wrapper allocates.

How it works:
  1. Wrapper opens a PTY and spawns the agent CLI on it as its own child
  2. Everything the agent writes is relayed to the wrapper's stdout, so the
     TUI renders in whatever terminal the wrapper itself is running in
  3. The same output is fed to a pyte terminal emulator, giving the login and
     quota watchers a rendered screen to poll (what `tmux capture-pane` used
     to provide)
  4. Queue watcher injects prompts by writing straight to the PTY master
  5. Anything typed at the wrapper's terminal is forwarded to the agent, so
     interactive logins can be completed by attaching to the wrapper

The wrapper used to start a *second* tmux session for the agent while the
agentchattr CLI already ran the wrapper itself inside one. That nesting meant
`agentchattr attach w1-claude` landed on wrapper log output rather than the
agent, and the real TUI lived under a separate, server-assigned session name.
With the agent on the wrapper's own PTY there is one session per agent and
attaching to it reaches the agent directly.
"""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

import pyte

# Used when the wrapper has no terminal of its own to copy dimensions from
# (fully daemonized, stdout not a tty). Wide enough that agent TUIs lay out
# their panels normally instead of falling back to a cramped 80x24.
DEFAULT_COLS, DEFAULT_ROWS = 120, 40


def _get_winsize(fd) -> tuple[int, int] | None:
    """Return (cols, rows) for a terminal fd, or None if it isn't one."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except (OSError, ValueError):
        return None
    rows, cols = struct.unpack("hhhh", packed)[:2]
    if rows <= 0 or cols <= 0:
        return None
    return cols, rows


def _set_winsize(fd, cols: int, rows: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", rows, cols, 0, 0))
    except (OSError, ValueError):
        pass


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError):
        return False


class AgentTerminal:
    """The PTY the agent CLI runs on, mirrored into a pyte screen.

    wrapper.py builds this before the agent starts, because the screen reader
    and activity checker are wired up ahead of ``run_agent``. Until the agent
    is spawned the terminal reports no screen, so the login/quota detectors
    stay idle rather than matching against a blank one.
    """

    def __init__(self, cols: int | None = None, rows: int | None = None):
        if cols is None or rows is None:
            size = _get_winsize(sys.stdout.fileno()) or (DEFAULT_COLS, DEFAULT_ROWS)
            cols, rows = size
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        # Guards the screen: the output relay feeds it from its own thread
        # while the watcher threads read it.
        self._lock = threading.Lock()
        self._master_fd: int | None = None
        self.pid: int | None = None
        # Monotonic counter of PTY output chunks.  Polling only the rendered
        # screen can miss a spinner/redraw that cycles back to the same frame
        # between polls, which made active agents intermittently look idle.
        self._output_generation = 0

    # -- lifecycle -------------------------------------------------------

    def attach_pty(self, master_fd: int) -> None:
        with self._lock:
            self._master_fd = master_fd
            self._screen.reset()

    def detach_pty(self) -> None:
        with self._lock:
            self._master_fd = None

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            self._screen.resize(rows, cols)
            if self._master_fd is not None:
                _set_winsize(self._master_fd, cols, rows)

    # -- output ----------------------------------------------------------

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._stream.feed(data)
            if data:
                self._output_generation += 1

    def read_output_generation(self) -> int:
        """Return a per-terminal counter that advances whenever the PTY writes."""
        with self._lock:
            return self._output_generation

    def read_screen(self) -> str | None:
        """Rendered visible screen, or None before the agent has started.

        Equivalent to what `tmux capture-pane -p` returned: escape sequences
        resolved, one line per terminal row. The login and quota detectors
        depend on this being a *screen* rather than a scrollback stream —
        they report "resolved" when a prompt is no longer on it.
        """
        with self._lock:
            if self._master_fd is None:
                return None
            return "\n".join(self._screen.display)

    # -- input -----------------------------------------------------------

    def write(self, data: bytes) -> bool:
        with self._lock:
            fd = self._master_fd
        if fd is None:
            return False
        try:
            os.write(fd, data)
            return True
        except OSError:
            return False

    def inject(self, text: str, delay: float = 0.3) -> None:
        """Type `text` into the agent, then press Enter.

        Enter is a separate write, as it was with `tmux send-keys`: agent CLIs
        read the prompt as it arrives, and sending the newline in the same
        write can submit before the whole prompt has been consumed. The gap
        scales with length so long prompts get more time.
        """
        if not self.write(text.encode("utf-8")):
            return
        time.sleep(max(delay, len(text) * 0.001))
        self.write(b"\r")


def inject(text: str, *, terminal: AgentTerminal, delay: float = 0.3):
    """Send text + Enter to the agent's PTY."""
    terminal.inject(text, delay=delay)


def get_screen_reader(terminal: AgentTerminal):
    """Return a callable that captures the agent's visible screen (or None)."""
    return terminal.read_screen


def get_activity_checker(terminal: AgentTerminal, trigger_flag=None):
    """Return a callable that detects recent agent PTY output.

    Activity is held through a few quiet polls so an agent does not flicker
    idle between intermittent TUI frames.  Each checker owns its state, which
    keeps multiple instances of the same CLI independent.
    """
    last_generation = [None]
    consecutive_idle = [0]
    active = [False]
    idle_cooldown = 5

    def check():
        # External trigger: queue watcher injected a message
        triggered = False
        if trigger_flag is not None and trigger_flag[0]:
            trigger_flag[0] = False
            triggered = True

        generation = terminal.read_output_generation()
        changed = (
            last_generation[0] is not None
            and generation != last_generation[0]
        )
        last_generation[0] = generation

        if changed or triggered:
            consecutive_idle[0] = 0
            active[0] = True
        else:
            consecutive_idle[0] += 1
            if consecutive_idle[0] >= idle_cooldown:
                active[0] = False

        return active[0]

    return check


def _child_setup():
    """Give the agent its own session with the PTY slave as controlling tty.

    Runs in the forked child before exec. Without a controlling terminal, TUI
    CLIs can't read keys or size themselves.
    """
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _spawn(argv, cwd, env, terminal):
    """Start the agent on a fresh PTY. Returns (proc, master_fd)."""
    master_fd, slave_fd = pty.openpty()
    size = _get_winsize(sys.stdout.fileno()) or (DEFAULT_COLS, DEFAULT_ROWS)
    _set_winsize(master_fd, *size)
    terminal.resize(*size)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_child_setup,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)
    terminal.attach_pty(master_fd)
    return proc, master_fd


def _write_stdout(data: bytes) -> None:
    """Write raw agent output to our stdout, whatever stdout happens to be.

    The agent's TUI is byte-oriented, so the binary buffer is the right target;
    a replaced stdout (test capture, some launchers) may not expose one, in
    which case a lossy text write still beats dropping the output.
    """
    try:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _relay_output(master_fd, terminal, stop):
    """Pump PTY output to our stdout and into the pyte screen."""
    while not stop.is_set():
        try:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            break
        if not data:
            break
        terminal.feed(data)
        _write_stdout(data)


def run_agent(
    command,
    extra_args,
    cwd,
    env,
    queue_file,
    agent,
    no_restart,
    start_watcher,
    strip_env=None,
    pid_holder=None,
    terminal=None,
    inject_env=None,
    inject_delay: float = 0.3,
    attach: bool = True,
):
    """Run the agent CLI on a PTY owned by this process.

    The agent is exec'd directly — no shell — so its arguments reach it as
    written, with no quoting round-trip. Environment changes (`strip_env`,
    `inject_env`) apply straight to the child's env instead of being encoded
    as an `env(1)` prefix, which was only ever needed because a multiplexer
    sat between the wrapper and the agent.

    Whether the session is interactive follows from stdin being a terminal,
    not from `attach`: when the agentchattr CLI runs the wrapper headless
    inside its own session, stdin is still that session's terminal, so
    attaching to it gives a fully usable agent — which is what makes an
    interactive login recoverable. `attach` only shapes the printed hints.
    """
    if terminal is None:
        terminal = AgentTerminal()

    child_env = dict(env)
    for var in strip_env or []:
        child_env.pop(var, None)
    child_env.update(inject_env or {})

    argv = [command, *extra_args]

    start_watcher(lambda text: terminal.inject(text, delay=inject_delay))

    interactive = _stdin_is_tty()
    if attach:
        print(f"  Running {agent} on this terminal (Ctrl+B, D detaches if you're in tmux)", flush=True)
    else:
        print(f"  Running {agent} headless — attach to this session to use it", flush=True)

    saved_termios = None
    prev_winch = None
    if interactive:
        stdin_fd = sys.stdin.fileno()
        try:
            saved_termios = termios.tcgetattr(stdin_fd)
            # Raw mode: keystrokes (including Ctrl+C, which belongs to the
            # agent, not the wrapper) pass through untouched.
            tty.setraw(stdin_fd)
        except termios.error:
            saved_termios = None

        def _on_winch(signum, frame):
            size = _get_winsize(sys.stdout.fileno())
            if size:
                terminal.resize(*size)

        try:
            prev_winch = signal.signal(signal.SIGWINCH, _on_winch)
        except (ValueError, OSError):
            prev_winch = None

    proc = None
    try:
        while True:
            try:
                proc, master_fd = _spawn(argv, cwd, child_env, terminal)
            except FileNotFoundError:
                print(f"\r\n  Error: command not found: {command}\r\n", flush=True)
                break
            except OSError as exc:
                print(f"\r\n  Error: failed to start {command}: {exc}\r\n", flush=True)
                break

            if pid_holder is not None:
                pid_holder[0] = proc.pid

            stop = threading.Event()
            relay = threading.Thread(
                target=_relay_output, args=(master_fd, terminal, stop), daemon=True
            )
            relay.start()

            try:
                while proc.poll() is None:
                    if not interactive:
                        time.sleep(0.2)
                        continue
                    try:
                        ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
                    except (OSError, ValueError):
                        interactive = False
                        continue
                    if not ready:
                        continue
                    try:
                        data = os.read(sys.stdin.fileno(), 65536)
                    except OSError:
                        continue
                    if data:
                        terminal.write(data)
            finally:
                stop.set()
                relay.join(timeout=1)
                terminal.detach_pty()
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                if pid_holder is not None:
                    pid_holder[0] = None

            if no_restart:
                break

            print(f"\r\n  {agent.capitalize()} exited.\r\n", flush=True)
            print("  Restarting in 3s... (Ctrl+C to quit)\r\n", flush=True)
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if prev_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, prev_winch)
            except (ValueError, OSError):
                pass
        if saved_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_termios)
            except termios.error:
                pass
