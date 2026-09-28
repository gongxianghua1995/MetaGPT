"""DockerTerminal: a Terminal subclass that runs every command via `docker exec` in a fixed container.

Used by SWEBenchEngineer so the LLM sees a `DockerTerminal.run_command` schema and naturally
produces commands executed inside the SWE-bench /testbed container, without touching the
upstream Terminal class.
"""
import asyncio
import time
import re
from asyncio import Queue
from asyncio.subprocess import PIPE, STDOUT

from metagpt.roles.di.swe_shell import docker_shell_argv
from metagpt.logs import logger
from metagpt.tools.libs.terminal import Terminal
from metagpt.tools.tool_registry import register_tool
from metagpt.utils.report import END_MARKER_VALUE, TerminalReporter


@register_tool()
class DockerTerminal(Terminal):
    """Terminal that runs every command via `docker exec` in a fixed container.

    Inherits Terminal only for type compatibility (so it can be injected where a Terminal is
    expected). All I/O is container-local; no persistent host shell is started.
    """

    def __init__(self, container_name: str, cwd: str = "/testbed"):
        # Intentionally skip Terminal.__init__ to avoid starting a host shell process.
        self.container_name = container_name
        self.docker_cwd = cwd
        self.command_terminator = "\n"
        self.pwd_command = "pwd"
        self.stdout_queue = Queue(maxsize=1000)
        self.observer = TerminalReporter()
        self.process = None  # unused; kept for Terminal API compatibility
        self.forbidden_commands = {
            "run dev": "Use Deployer.deploy_to_public instead.",
            "serve ": "Use Deployer.deploy_to_public instead.",
        }
        self._docker_ready = False
        self.execution_deadline = None
        self.trace_state = None
        self.trace_role = ""

    async def _check_container(self):
        """Verify the target container is running before issuing any exec."""
        check = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", self.container_name,
            stdout=PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await check.communicate()
        if b"true" not in out:
            raise RuntimeError(
                f"Docker container {self.container_name} not running. "
                f"Start it in refresh_repo first: docker run -d --name {self.container_name} "
                f"-w {self.docker_cwd} <image> sleep 2h"
            )
        self._docker_ready = True

    async def run_command(self, cmd: str, daemon: bool = False) -> str:
        """Execute a shell command inside the docker container via `docker exec`.

        Args:
            cmd (str): The shell command to execute in the container.
            daemon (bool): If True, runs in a background task and returns "" immediately.

        Returns:
            str: The command's stdout (and stderr, merged).
        """
        if not self._docker_ready:
            await self._check_container()

        output = ""
        # Remove forbidden commands (mirrors upstream Terminal.run_command behavior)
        # The SWE container is already isolated. Generic web-app command
        # substring filters corrupt quoted source text (e.g. "preserve ").
        # Execute the supplied shell program verbatim.


        return output + await self._run_command_docker(cmd, daemon)

    async def _run_command_docker(self, cmd: str, daemon: bool) -> str:
        result = await self.run_with_status(cmd)
        return result['output'] + ("\n[exit code: %s]" % result['returncode'] if result['returncode'] else "")

    async def run_with_status(self, cmd: str, timeout: float = 60) -> dict:
        """Execute the original shell program verbatim, with a bounded lifetime."""
        seconds = max(1, min(timeout, self.execution_deadline - time.monotonic())) if self.execution_deadline else timeout
        argv = docker_shell_argv(self.container_name, self.docker_cwd, cmd, seconds)
        if self.trace_state:
            self.trace_state.record('role_action_started', role=self.trace_role, command=cmd)
        proc = await asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=STDOUT)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds + 3)
            result = dict(output=output.decode(errors='replace'), returncode=proc.returncode)
        except asyncio.TimeoutError:
            result = dict(output='Command timed out', returncode=124)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
        if self.trace_state:
            self.trace_state.record('role_action', role=self.trace_role, command=cmd, result=result)
        return result

    async def _read_docker_output(self, proc, cmd, daemon: bool = False) -> str:
        """Read docker exec subprocess output until END_MARKER (or EOF if the proc exits)."""
        async with self.observer as observer:
            cmd_output = []
            await observer.async_report(cmd + self.command_terminator, "cmd")
            tmp = b""
            while True:
                incoming = await proc.stdout.read(1)
                if not incoming:
                    # EOF with a non-newline tail used to replay tmp forever,
                    # starving the event loop and even asyncio deadlines.
                    if tmp:
                        line = tmp.decode(errors="ignore")
                        await observer.async_report(line, "output")
                        cmd_output.append(line)
                    break
                chunk = tmp + incoming
                *lines, rest = chunk.splitlines(True)
                # splitlines(True) keeps the trailing newline on each line; if the last
                # fragment ends with a newline it is a complete line, otherwise buffer it.
                if rest and rest.endswith(b"\n"):
                    lines = lines + [rest]
                    tmp = b""
                else:
                    tmp = rest
                for line in lines:
                    line = line.decode(errors="ignore")
                    ix = line.rfind(END_MARKER_VALUE)
                    if ix >= 0:
                        line = line[:ix]
                        if line:
                            await observer.async_report(line, "output")
                            cmd_output.append(line)
                        return "".join(cmd_output)
                    await observer.async_report(line, "output")
                    cmd_output.append(line)
                    if daemon:
                        await self.stdout_queue.put(line)
            return "".join(cmd_output)

    async def _start_process(self):
        # No host shell to start; just verify the container.
        await self._check_container()

    async def close(self):
        """Nothing to close: the container lifecycle is owned by refresh_repo."""
        return
