"""PTY session manager: spawns and drives one interactive CLI per agent.

Cross-platform:
  - Windows: pywinpty (ConPTY)
  - POSIX:   built-in pty + os.fork via pty.spawn-like manual loop

Each PtySession runs the agent's CLI as a persistent interactive process and
exposes async write()/read via an output callback.
"""
from __future__ import annotations

import asyncio
import os
import sys
import shlex

from . import runtimes

IS_WIN = sys.platform == "win32"

# Backward-compatible view of the historical launch table, now derived from the
# runtime adapter registry (Cut 7). Kept so existing callers/tests that read
# ``DEFAULT_CMDS`` keep working; the registry is the source of truth.
DEFAULT_CMDS = {a.id: list(a.base_argv) for a in runtimes.all_adapters()}


def resolve_cmd(runtime: str, launch_cmd: str | None,
                model: str | None = None,
                permission_mode: str | None = None) -> list[str]:
    """Resolve the launch argv for an agent.

    An explicit ``launch_cmd`` override still wins (split without a shell and
    validated). Otherwise the runtime adapter registry builds the exact argv
    for ``(runtime, model, permission_mode)``. An unknown runtime falls back to
    the mock runtime, preserving the historical default.
    """
    if launch_cmd:
        argv = shlex.split(launch_cmd, posix=not IS_WIN)
        return runtimes.validate_argv(argv)
    if not runtimes.has(runtime):
        runtime = "mock"
    return runtimes.build_command(runtime, model=model,
                                  permission_mode=permission_mode)



class PtySession:
    def __init__(self, cmd: list[str], cwd: str | None, on_output, on_exit,
                 cols: int = 120, rows: int = 30):
        self.cmd = cmd
        self.cwd = cwd or None
        self.on_output = on_output   # async fn(str)
        self.on_exit = on_exit       # async fn(int)
        self.cols = cols
        self.rows = rows
        self._loop = asyncio.get_event_loop()
        self._alive = False

    async def start(self):
        if IS_WIN:
            await self._start_win()
        else:
            await self._start_posix()

    # -------- Windows (pywinpty) --------
    async def _start_win(self):
        import winpty  # type: ignore
        self._pty = winpty.PtyProcess.spawn(
            self.cmd, cwd=self.cwd, dimensions=(self.rows, self.cols))
        self._alive = True
        asyncio.create_task(self._win_reader())

    async def _win_reader(self):
        loop = self._loop
        while self._alive:
            try:
                data = await loop.run_in_executor(None, self._pty.read, 4096)
            except EOFError:
                data = ""
            if data == "" and not self._pty.isalive():
                break
            if data:
                await self.on_output(data)
        self._alive = False
        code = self._pty.exitstatus or 0
        await self.on_exit(code)

    # -------- POSIX (pty) --------
    async def _start_posix(self):
        import pty
        pid, fd = pty.fork()
        if pid == 0:  # child
            if self.cwd:
                try:
                    os.chdir(self.cwd)
                except Exception:
                    pass
            os.execvp(self.cmd[0], self.cmd)
            os._exit(127)
        self._pid = pid
        self._fd = fd
        self._alive = True
        self.resize(self.cols, self.rows)
        asyncio.create_task(self._posix_reader())

    async def _posix_reader(self):
        loop = self._loop
        while self._alive:
            try:
                data = await loop.run_in_executor(None, os.read, self._fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            await self.on_output(data.decode(errors="replace"))
        self._alive = False
        try:
            _, status = os.waitpid(self._pid, 0)
            code = os.WEXITSTATUS(status)
        except Exception:
            code = 0
        await self.on_exit(code)

    def write(self, data: str):
        if not self._alive:
            return
        if IS_WIN:
            self._pty.write(data)
        else:
            os.write(self._fd, data.encode())

    def resize(self, cols: int, rows: int):
        try:
            if IS_WIN:
                self._pty.setwinsize(rows, cols)
            else:
                import fcntl, termios, struct
                fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def kill(self):
        self._alive = False
        try:
            if IS_WIN:
                self._pty.terminate(force=True)
            else:
                os.kill(self._pid, 9)
        except Exception:
            pass
