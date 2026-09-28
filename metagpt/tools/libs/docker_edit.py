"""DockerEdit: semantic text replacement inside a docker container.

SWE-agent's `edit <start>:<end> <<EOF ... EOF` is a formatting trap for weak
models: the heredoc body (multi-line Python code) must be JSON-escaped
(\\n, \") while line numbers stay consistent — 14 smoke runs produced only 3
valid edit commands, all after multiple JSON repairs. Given the choice, models
*prefer* semantic replacement (smoke8: hallucinated
Editor.edit_file_by_replace(file, to_replace, new_content)).

DockerEdit exposes that preferred interface against the container: three plain
string fields, copy-paste friendly, no escaping gymnastics. Replacement is
exact-match and must be unique, so a wrong `old` fails loudly with actionable
feedback instead of silently corrupting the file.
"""
import asyncio
import base64

from pydantic import BaseModel

from metagpt.logs import logger
from metagpt.tools.tool_registry import register_tool


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@register_tool(include_functions=["replace", "multi_replace"])
class DockerEdit(BaseModel):
    """Exact-match semantic replacement of source code inside the container.

    Don't initialize a new instance of this class if one already exists.
    """

    container_name: str
    cwd: str = "/testbed"

    async def replace(self, file: str, old: str, new: str) -> str:
        """Replace the FIRST-and-ONLY occurrence of `old` with `new` in `file` (inside the container).

        Args:
            file (str): File to edit, e.g. "django/db/backends/mysql/operations.py".
            old (str): Exact existing text to replace — copy it verbatim (with indentation) from the last open/cat output; it must appear exactly once in the file.
            new (str): Replacement text with correct indentation.

        Returns:
            str: REPLACE_OK with the patched path, or REPLACE_FAILED with the reason and how to fix the call.
        """
        # Resolve relative paths against the working dir; refuse escapes.
        if not file.startswith("/"):
            file = f"{self.cwd.rstrip('/')}/{file}"
        if not file.startswith(self.cwd):
            return f"REPLACE_FAILED: {file} is outside {self.cwd}"

        script = (
            "import base64, sys\n"
            f"path = base64.b64decode('{_b64(file)}').decode()\n"
            f"old = base64.b64decode('{_b64(old)}').decode()\n"
            f"new = base64.b64decode('{_b64(new)}').decode()\n"
            "try:\n"
            "    src = open(path).read()\n"
            "except FileNotFoundError:\n"
            "    if old:\n"
            "        print('REPLACE_FAILED: file not found: ' + path)\n"
            "        sys.exit(0)\n"
            "    open(path, 'w').write(new)\n"
            "    print('REPLACE_OK: created ' + path)\n"
            "    sys.exit(0)\n"
            "if not old:\n"
            "    print('REPLACE_FAILED: empty old is only valid when creating a new file')\n"
            "    sys.exit(0)\n"
            "n = src.count(old)\n"
            "if n == 0:\n"
            "    print('REPLACE_FAILED: old text not found in ' + path +\n"
            "          ' — copy it verbatim (including indentation) from the last open/cat output')\n"
            "    sys.exit(0)\n"
            "if n > 1:\n"
            "    print(f'REPLACE_FAILED: old text matches {n} places in ' + path +\n"
            "          ' — include more surrounding lines to make it unique')\n"
            "    sys.exit(0)\n"
            "open(path, 'w').write(src.replace(old, new))\n"
            "print('REPLACE_OK: patched ' + path)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.container_name,
            "python3", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        result = out.decode(errors="ignore").strip()
        logger.info(f"DockerEdit.replace {file}: {result[:80]}")
        return result

    async def multi_replace(self, files: list[str], olds: list[str], news: list[str]) -> str:
        """Apply N exact-match replacements in ONE call. All three lists must
        have the same length. Each olds[i] must appear exactly once in files[i].
        Replacements are applied sequentially; if any one fails, the run stops
        and reports which index failed (already-applied edits stay).

        Use this when a fix needs coordinated edits across 2+ files or 2+ sites
        in one file — it avoids the JSON formatting tax of N separate
        DockerEdit.replace calls.

        Args:
            files (list[str]): List of file paths, e.g. ["a.py", "b.py", "a.py"].
                Relative paths resolve against /testbed. Same file may appear
                multiple times (edits apply in order).
            olds (list[str]): List of exact existing texts to replace. Copy each
                verbatim (with indentation) from the last open/cat output; each
                must appear exactly once in its file at apply time.
            news (list[str]): List of replacement texts with correct indentation.

        Returns:
            str: MULTI_REPLACE_OK with count, or MULTI_REPLACE_FAILED with the
            failing index and reason.
        """
        if not (len(files) == len(olds) == len(news)):
            return (
                "MULTI_REPLACE_FAILED: length mismatch — "
                f"files={len(files)} olds={len(olds)} news={len(news)}"
            )
        if not files:
            return "MULTI_REPLACE_FAILED: empty edit list"

        # Resolve paths and refuse escapes up front.
        resolved = []
        for f in files:
            if not f.startswith("/"):
                f = f"{self.cwd.rstrip('/')}/{f}"
            if not f.startswith(self.cwd):
                return f"MULTI_REPLACE_FAILED: {f} is outside {self.cwd}"
            resolved.append(f)

        # Serialize the whole edit list as base64 and run a single python3
        # process in the container. This keeps the JSON the model has to emit
        # flat (three parallel string lists) while the execution side handles
        # arbitrary text safely.
        import json as _json
        payload = _json.dumps([
            {"file": r, "old": o, "new": n}
            for r, o, n in zip(resolved, olds, news)
        ])
        payload_b64 = _b64(payload)

        script = (
            "import base64, sys, json\n"
            f"edits = json.loads(base64.b64decode('{payload_b64}').decode())\n"
            "applied = 0\n"
            "for i, e in enumerate(edits):\n"
            "    path, old, new = e['file'], e['old'], e['new']\n"
            "    try:\n"
            "        src = open(path).read()\n"
            "    except FileNotFoundError:\n"
            "        print(f'MULTI_REPLACE_FAILED at edit {i}: file not found: ' + path)\n"
            "        sys.exit(0)\n"
            "    n = src.count(old)\n"
            "    if n == 0:\n"
            "        print(f'MULTI_REPLACE_FAILED at edit {i}: old text not found in ' + path +\n"
            "              ' — copy it verbatim (including indentation) from the last open/cat output')\n"
            "        sys.exit(0)\n"
            "    if n > 1:\n"
            "        print(f'MULTI_REPLACE_FAILED at edit {i}: old text matches {n} places in ' + path +\n"
            "              ' — include more surrounding lines to make it unique')\n"
            "        sys.exit(0)\n"
            "    open(path, 'w').write(src.replace(old, new))\n"
            "    applied += 1\n"
            "print(f'MULTI_REPLACE_OK: applied {applied}/{len(edits)} edits')\n"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.container_name,
            "python3", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        result = out.decode(errors="ignore").strip()
        logger.info(f"DockerEdit.multi_replace ({len(files)} edits): {result[:120]}")
        return result
