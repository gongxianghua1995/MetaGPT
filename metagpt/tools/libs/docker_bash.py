"""DockerBash: a Bash subclass with a persistent shell inside a docker container.

The upstream Terminal maintains a long-running local `bash` process via
asyncio subprocess pipes. SWE-agent's custom functions (open / goto / edit /
submit / state) rely on this persistence: `open` sets $CURRENT_FILE, `edit`
reads it. Without a persistent shell, every command runs in a fresh process
and these variables are lost.

DockerBash overrides `_start_process` to spawn `docker exec -i bash` as a
persistent asyncio subprocess — identical pipe-based I/O as Terminal, but the
shell lives inside the container. The LLM sees the same `Bash.run` schema; all
open/goto/edit/submit/state calls now maintain state across invocations.
"""
import asyncio
import logging
import os
import re
import subprocess
import sys
from asyncio.subprocess import PIPE, STDOUT

from metagpt.const import get_metagpt_package_root
from metagpt.tools.libs.terminal import Bash
from metagpt.utils.report import END_MARKER_VALUE

logger = logging.getLogger(__name__)

# Per-command wall-clock cap. Weak models periodically emit commands with an
# unterminated quote / dangling line continuation; non-interactive `bash -l`
# then sits at PS2 waiting for more stdin, the END marker never arrives, and
# the read loop (and the whole runner) hangs forever. On timeout we abort the
# wedged exec session and spawn a fresh shell instead of hanging the case.
CMD_TIMEOUT_SECONDS = 240


class DockerBash(Bash):
    """Bash with a persistent shell running inside a fixed docker container.

    Overrides _start_process (spawns `docker exec -i bash` instead of local
    `bash`), start (copies setup scripts + sources them), and run_command
    (adds a trailing echo to flush the marker line — see comment below).
    """

    def __init__(self, container_name: str, cwd: str = "/testbed"):
        self.container_name = container_name
        self.initial_patch = ""
        self.cwd = cwd
        super(Bash, self).__init__()
        self.start_flag = False

    async def _start_process(self):
        self.process = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "-w", self.cwd, self.container_name,
            "bash", "-l",
            stdin=PIPE,
            stdout=PIPE,
            stderr=STDOUT,
            env=os.environ.copy(),
        )

    async def start(self):
        host_cmds_dir = get_metagpt_package_root() / "metagpt/tools/swe_agent_commands"
        subprocess.run(
            ["docker", "cp", str(host_cmds_dir), f"{self.container_name}:/tmp/swe_commands/"],
            check=True,
            capture_output=True,
            text=True,
        )
        # edit_linting.sh gates every `edit` on a flake8 diff between the
        # original and modified file. Eval containers ship no flake8, so both
        # runs emit "<script>: line N: flake8: command not found"; after the
        # script's line:col normalization these still differ ("line 51" vs
        # "line 81"), making EVERY edit report a bogus "new syntax error" —
        # while the write has already landed (no rollback). Weak models then
        # burn rounds retrying identical edits. Provide a silent no-op shim in
        # front of PATH (this dir is prepended by the setup below): both lint
        # outputs become empty, cmp passes, and edits apply cleanly.
        shim = "#!/bin/bash\nexit 0\n"
        subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c",
             f'printf "{shim}" > /tmp/swe_commands/flake8 && chmod +x /tmp/swe_commands/flake8'],
            check=True, capture_output=True,
        )
        setup_cmd = (
            f"cd {self.cwd} && "
            f"export SWE_CMD_WORK_DIR={self.cwd} && "
            f"source /tmp/swe_commands/_setup_default_env.sh && "
            f"export PATH=$PATH:/tmp/swe_commands && "
            f"source /tmp/swe_commands/defaults.sh && "
            f"source /tmp/swe_commands/search.sh && "
            f"source /tmp/swe_commands/edit_linting.sh"
        )
        await self.run_command(setup_cmd)
        self.start_flag = True

    async def run_command(self, cmd: str, daemon=False) -> str:
        """Override of Terminal.run_command with a Docker-specific fix.

        In the local Terminal, interactive bash prints a prompt (PS1) after
        each command, so the END_MARKER line is always followed by more bytes.
        The read loop's `*lines, tmp = output.splitlines(True)` then moves the
        marker line from tmp into lines, where it gets detected.

        Docker's non-interactive `bash -l` produces NO prompt, so the marker
        is the last output and stays trapped in tmp forever — the read loop
        hangs. Adding a trailing `echo` after the marker guarantees one more
        newline, forcing splitlines to process the marker line.
        """
        if self.process is None:
            await self._start_process()

        output = ""
        # The SWE container is already isolated. Generic web-app command
        # substring filters corrupt quoted source text (e.g. "preserve ").
        # Execute the supplied shell program verbatim.


        self.process.stdin.write((cmd + self.command_terminator).encode())
        marker_cmd = f"echo {END_MARKER_VALUE}"
        self.process.stdin.write((marker_cmd + self.command_terminator).encode())
        self.process.stdin.write(b"echo\n")
        await self.process.stdin.drain()

        if daemon:
            asyncio.create_task(self._read_and_process_output(cmd))
            return output

        try:
            output += await asyncio.wait_for(
                self._read_and_process_output(cmd), timeout=CMD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"DockerBash command timed out after {CMD_TIMEOUT_SECONDS}s "
                f"(container={self.container_name}); restarting shell. "
                f"cmd[:120]={cmd[:120]!r}"
            )
            await self._restart_shell()
            output += (
                f"\n[TIMEOUT] Command exceeded {CMD_TIMEOUT_SECONDS}s and the shell "
                "was restarted. This is usually caused by an unterminated quote or a "
                "dangling multi-line continuation leaving bash waiting for input. "
                "Re-issue the command as a SINGLE line with every quote closed; for "
                "multi-line edits use DockerEdit.replace instead.\n"
            )
        except EOFError as e:
            logger.warning(
                f"DockerBash shell died (EOF) — restarting shell "
                f"(container={self.container_name}). {e}"
            )
            await self._restart_shell()
            output += (
                "\n[SHELL DIED] The shell process exited unexpectedly — usually an "
                "invalid multi-line command (unterminated quote / stray backslash "
                "continuation). The shell was restarted; your command did NOT "
                "complete. Re-issue it as a SINGLE line, or use "
                "DockerEdit.replace for multi-line edits.\n"
            )

        return output

    async def run_independent(self, cmd: str, timeout: int = 30) -> dict:
        """Control operations must not reuse a cancelled shell's output stream."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-w", self.cwd, self.container_name,
            "timeout", "-k", "2", str(timeout), "bash", "-c", cmd,
            stdout=PIPE, stderr=PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return {"output": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace"),
                "exit_code": proc.returncode}

    async def collect_patch(self) -> str:
        """Collect the live tree with an independent exec and checked exit code.

        A persistent tool shell can contain unread output after cancellation,
        or be stuck inside an unfinished command. Neither may turn a valid
        patch into a silently empty prediction.
        """
        result = await self.run_independent(
            "printf '\n.backup.*\n/patch.txt\n' >> .git/info/exclude && "
            "git add -A && git --no-pager diff --cached")
        if result["exit_code"] != 0:
            raise RuntimeError("Patch collection failed: " + result["stderr"])
        # Images sometimes contain tracked compatibility edits already.
        # Remove only byte-identical, unchanged baseline file diffs. If the
        # agent edits such a file, retain its complete HEAD-relative diff.
        initial_files = set(re.split(r"(?=^diff --git )", self.initial_patch, flags=re.MULTILINE))
        return "".join(part for part in re.split(r"(?=^diff --git )", result["output"], flags=re.MULTILINE)
                       if part not in initial_files)

    async def run_with_status(self, cmd: str) -> dict:
        import time
        import uuid
        marker = "__SWE_EXIT_" + uuid.uuid4().hex + "__"
        started = time.monotonic()
        output = await self.run(cmd + "\nprintf '\\n" + marker + "%s\\n' \"$?\"")
        body, sep, status = output.rpartition(marker)
        try:
            exit_code = int(status.strip()) if sep else None
        except ValueError:
            exit_code = None
        return {"command": cmd, "output": body.rstrip("\n") if sep else output,
                "exit_code": exit_code, "duration_seconds": round(time.monotonic() - started, 3)}

    async def _restart_shell(self):
        """Kill the wedged `docker exec` session and spawn a fresh shell.

        When bash is stuck at PS2 reading stdin, killing the docker exec
        client closes the stdin pipe and the in-container bash exits on EOF,
        so no orphan is left behind. Shell state ($CURRENT_FILE etc.) is lost;
        start() re-sources the SWE helper scripts to restore functions.
        """
        proc = self.process
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
        self.process = None
        self.start_flag = False
        await self._start_process()
        await self.start()
