"""DockerEditor: an Editor subclass whose file IO primitives go through `docker exec`.

Used by SWEBenchEngineer so all DockerEditor.* commands operate on /testbed inside the
SWE-bench container, without modifying the upstream Editor.py. The LLM sees
`DockerEditor.write` / `DockerEditor.edit_file_by_replace` schemas and naturally produces
container-local commands.

Inherits Editor only for pydantic type compatibility (so it can be injected where an Editor
is expected, e.g. RoleZero.editor). All IO methods are overridden to go through `docker exec`;
no host filesystem is touched.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union

import tiktoken

from metagpt.const import DEFAULT_MIN_TOKEN_COUNT
from metagpt.tools.libs.editor import (
    ERROR_GUIDANCE,
    Editor,
    FileBlock,
    INDENTATION_INFO,
    LINE_NUMBER_AND_CONTENT_MISMATCH,
    LINTER_ERROR_MSG,
    LineNumberError,
    SUCCESS_EDIT_INFO,
)
from metagpt.tools.libs.linter import Linter
from metagpt.tools.tool_registry import register_tool


@register_tool(
    include_functions=[
        "write",
        "read",
        "open_file",
        "goto_line",
        "scroll_down",
        "scroll_up",
        "create_file",
        "edit_file_by_replace",
        "insert_content_at_line",
        "append_file",
        "search_dir",
        "search_file",
        "find_file",
        "similarity_search",
    ]
)
class DockerEditor(Editor):
    """Editor whose every file operation runs inside a fixed docker container.

    Inherits Editor for type compatibility; overrides all IO to go through `docker exec`.
    """

    container_name: str = ""
    cwd: str = "/testbed"

    # ------------------------------------------------------------------
    # IO primitives (docker exec based)
    # ------------------------------------------------------------------
    def _docker_exec(self, cmd: str, timeout: int = 60, combine_stderr: bool = False) -> str:
        import shlex
        import subprocess
        args = ["docker", "exec", "-w", self.cwd, self.container_name, "bash", "-lc", cmd]
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout + (r.stderr if combine_stderr else "")

    def _read_file(self, path) -> str:
        import shlex
        return self._docker_exec(f"cat {shlex.quote(str(path))}")

    def _write_file(self, path, content) -> None:
        import shlex
        import subprocess
        subprocess.run(
            ["docker", "exec", "-i", "-w", self.cwd, self.container_name, "bash", "-lc", f"cat > {shlex.quote(str(path))}"],
            input=content, text=True, capture_output=True, timeout=60,
        )

    def _file_exists(self, path) -> bool:
        import shlex
        import subprocess
        r = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-lc", f"test -f {shlex.quote(str(path))} && echo y || echo n"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() == "y"

    def _file_size(self, path) -> int:
        import shlex
        import subprocess
        r = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-lc", f"stat -c %s {shlex.quote(str(path))}"],
            capture_output=True, text=True, timeout=10,
        )
        try:
            return int(r.stdout.strip())
        except Exception:
            return 0

    def _is_dir(self, path) -> bool:
        import shlex
        import subprocess
        r = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-lc", f"test -d {shlex.quote(str(path))} && echo y || echo n"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() == "y"

    def _mkdir(self, path) -> None:
        import shlex
        self._docker_exec(f"mkdir -p {shlex.quote(str(path))}")

    def _try_fix_path(self, path: Union[Path, str]) -> Path:
        if not isinstance(path, Path):
            path = Path(path)
        # container mode: paths are container-local (e.g. /testbed/X); do NOT map to host.
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return path

    # ------------------------------------------------------------------
    # Public file operations (signatures match upstream Editor)
    # ------------------------------------------------------------------
    def write(self, path: str, content: str):
        """Write the whole content to a file. When used, make sure content arg contains the full content of the file."""
        path = self._try_fix_path(path)
        if "\n" not in content and "\\n" in content:
            content = content.replace("\\n", "\n")
        self._write_file(path, content)
        return f"The writing/coding the of the file {os.path.basename(str(path))}' is now completed. The file '{os.path.basename(str(path))}' has been successfully created."

    async def read(self, path: str) -> FileBlock:
        """Read the whole content of a file. Using absolute paths as the argument for specifying the file location."""
        path = self._try_fix_path(path)
        error = FileBlock(
            file_path=str(path),
            block_content="The file is too large to read. Use `DockerEditor.similarity_search` to read the file instead.",
        )
        if self._file_size(path) > 5 * DEFAULT_MIN_TOKEN_COUNT:
            return error
        content = self._read_file(path)
        if not content:
            return FileBlock(file_path=str(path), block_content="")
        if self.is_large_file(content=content):
            return error
        self.resource.report(str(path), "path")
        lines = content.splitlines(keepends=True)
        lines_with_num = [f"{i + 1:03}|{line}" for i, line in enumerate(lines)]
        return FileBlock(file_path=str(path), block_content="".join(lines_with_num))

    @staticmethod
    def _is_valid_filename(file_name: str) -> bool:
        if not file_name or not file_name.strip():
            return False
        invalid_chars = '<>:"/\\|?*' if os.name != "posix" else "\0"
        for char in invalid_chars:
            if char in file_name:
                return False
        return True

    def _is_valid_path(self, path: Path) -> bool:
        try:
            return self._file_exists(path)
        except Exception:
            return False

    def _create_paths(self, file_path: Path) -> bool:
        try:
            if file_path.parent:
                self._mkdir(file_path.parent)
            return True
        except Exception:
            return False

    def _check_current_file(self, file_path: Optional[Path] = None) -> bool:
        if file_path is None:
            file_path = self.current_file
        if not file_path or not self._file_exists(file_path):
            raise ValueError("No file open. Use the open_file function first.")
        return True

    @staticmethod
    def _clamp(value, min_value, max_value):
        return max(min_value, min(value, max_value))

    def _lint_file(self, file_path: Path) -> Tuple[Optional[str], Optional[int]]:
        """Lint a container file by copying it to a host temp file and running the host Linter."""
        import tempfile
        try:
            content = self._read_file(file_path)
            suffix = "".join(Path(str(file_path)).suffixes) or ".py"
            with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, dir="/tmp") as tf:
                tf.write(content)
                local_temp = tf.name
            try:
                linter = Linter(root=str(Path(local_temp).parent))
                lint_error = linter.lint(local_temp)
                if lint_error:
                    lint_error.text = lint_error.text.replace(local_temp, str(file_path))
            except Exception:
                # tree_sitter may raise TypeError on some Python versions; lint failure
                # must not block the edit flow.
                return None, None
            finally:
                os.unlink(local_temp)
        except Exception:
            return None, None
        if not lint_error:
            return None, None
        return "ERRORS:\n" + lint_error.text, lint_error.lines[0]

    def _print_window(self, file_path: Path, targeted_line: int, window: int):
        self._check_current_file(file_path)
        content = self._read_file(file_path)
        if not content.endswith("\n"):
            content += "\n"
        lines = content.splitlines(True)
        total_lines = len(lines)
        self.current_line = self._clamp(targeted_line, 1, total_lines)
        half_window = max(1, window // 2)
        start = max(1, self.current_line - half_window)
        end = min(total_lines, self.current_line + half_window)
        if start == 1:
            end = min(total_lines, start + window - 1)
        if end == total_lines:
            start = max(1, end - window + 1)
        output = ""
        if start > 1:
            output += f"({start - 1} more lines above)\n"
        else:
            output += "(this is the beginning of the file)\n"
        for i in range(start, end + 1):
            _new_line = f"{i:03d}|{lines[i - 1]}"
            if not _new_line.endswith("\n"):
                _new_line += "\n"
            output += _new_line
        if end < total_lines:
            output += f"({total_lines - end} more lines below)\n"
        else:
            output += "(this is the end of the file)\n"
        return output.rstrip()

    @staticmethod
    def _cur_file_header(current_file: Path, total_lines: int) -> str:
        if not current_file:
            return ""
        return f"[File: {current_file} ({total_lines} lines total)]\n"

    def _set_workdir(self, path: str) -> None:
        """Set the working directory (container path)."""
        self.cwd = path

    def open_file(self, path: Union[Path, str], line_number: Optional[int] = 1, context_lines: Optional[int] = None) -> str:
        """Opens the file at the given path in the editor."""
        if context_lines is None:
            context_lines = self.window
        path = self._try_fix_path(path)
        if not self._file_exists(path):
            raise FileNotFoundError(f"File {path} not found")
        self.current_file = path
        total_lines = max(1, len(self._read_file(path).splitlines()))
        if not isinstance(line_number, int) or line_number < 1 or line_number > total_lines:
            raise ValueError(f"Line number must be between 1 and {total_lines}")
        self.current_line = line_number
        if context_lines is None or context_lines < 1:
            context_lines = self.window
        output = self._cur_file_header(path, total_lines)
        output += self._print_window(path, self.current_line, self._clamp(context_lines, 1, 2000))
        self.resource.report(path, "path")
        return output

    def goto_line(self, line_number: int) -> str:
        """Moves the window to show the specified line number."""
        self._check_current_file()
        total_lines = max(1, len(self._read_file(self.current_file).splitlines()))
        if not isinstance(line_number, int) or line_number < 1 or line_number > total_lines:
            raise ValueError(f"Line number must be between 1 and {total_lines}")
        self.current_line = self._clamp(line_number, 1, total_lines)
        output = self._cur_file_header(self.current_file, total_lines)
        output += self._print_window(self.current_file, self.current_line, self.window)
        return output

    def scroll_down(self) -> str:
        """Moves the window down by 100 lines."""
        self._check_current_file()
        total_lines = max(1, len(self._read_file(self.current_file).splitlines()))
        self.current_line = self._clamp(self.current_line + self.window, 1, total_lines)
        output = self._cur_file_header(self.current_file, total_lines)
        output += self._print_window(self.current_file, self.current_line, self.window)
        return output

    def scroll_up(self) -> str:
        """Moves the window up by 100 lines."""
        self._check_current_file()
        total_lines = max(1, len(self._read_file(self.current_file).splitlines()))
        self.current_line = self._clamp(self.current_line - self.window, 1, total_lines)
        output = self._cur_file_header(self.current_file, total_lines)
        output += self._print_window(self.current_file, self.current_line, self.window)
        return output

    async def create_file(self, filename: str) -> str:
        """Creates and opens a new file with the given name."""
        filename = self._try_fix_path(filename)
        if self._file_exists(filename):
            raise FileExistsError(f"File '{filename}' already exists.")
        self._write_file(filename, "\n")
        self.open_file(filename)
        return f"[File {filename} created.]"

    # ------------------------------------------------------------------
    # edit/insert/append (memory backup pattern, no host temp file)
    # ------------------------------------------------------------------
    @staticmethod
    def _append_impl(lines, content):
        content_lines = content.splitlines(keepends=True)
        n_added_lines = len(content_lines)
        if lines and not (len(lines) == 1 and lines[0].strip() == ""):
            if not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            new_lines = lines + content_lines
            content = "".join(new_lines)
        else:
            content = "".join(content_lines)
        return content, n_added_lines

    @staticmethod
    def _insert_impl(lines, start, content):
        inserted_lines = [content + "\n" if not content.endswith("\n") else content]
        if len(lines) == 0:
            new_lines = inserted_lines
        elif start is not None:
            if len(lines) == 1 and lines[0].strip() == "":
                lines = []
            if len(lines) == 0:
                new_lines = inserted_lines
            else:
                new_lines = lines[: start - 1] + inserted_lines + lines[start - 1:]
        else:
            raise LineNumberError(
                f"Invalid line number: {start}. Line numbers must be between 1 and {len(lines)} (inclusive)."
            )
        content = "".join(new_lines)
        n_added_lines = len(inserted_lines)
        return content, n_added_lines

    @staticmethod
    def _edit_impl(lines, start, end, content):
        if start is None:
            start = 1
        if end is None:
            end = len(lines)
        if not (1 <= start <= len(lines)):
            raise LineNumberError(
                f"Invalid start line number: {start}. Line numbers must be between 1 and {len(lines)} (inclusive)."
            )
        if not (1 <= end <= len(lines)):
            raise LineNumberError(
                f"Invalid end line number: {end}. Line numbers must be between 1 and {len(lines)} (inclusive)."
            )
        if start > end:
            raise LineNumberError(f"Invalid line range: {start}-{end}. Start must be less than or equal to end.")
        if not content.endswith("\n"):
            content += "\n"
        content_lines = content.splitlines(True)
        n_added_lines = len(content_lines)
        new_lines = lines[: start - 1] + content_lines + lines[end:]
        if len(lines) == 0:
            new_lines = content_lines
        content = "".join(new_lines)
        return content, n_added_lines

    def _get_indentation_info(self, content, first_line):
        content_lines = content.split("\n")
        pre_line = content_lines[first_line - 2] if first_line - 2 >= 0 else ""
        pre_line_indent = len(pre_line) - len(pre_line.lstrip())
        insert_line = content_lines[first_line - 1]
        insert_line_indent = len(insert_line) - len(insert_line.lstrip())
        return INDENTATION_INFO.format(
            pre_line=pre_line,
            pre_line_indent=pre_line_indent,
            insert_line=insert_line,
            insert_line_indent=insert_line_indent,
            sub_4_space=max(insert_line_indent - 4, 0),
            add_4_space=insert_line_indent + 4,
        )

    def _edit_file_impl(
        self,
        file_name: Path,
        start: Optional[int] = None,
        end: Optional[int] = None,
        content: str = "",
        is_insert: bool = False,
        is_append: bool = False,
    ) -> str:
        ERROR_MSG = f"[Error editing file {file_name}. Please confirm the file is correct.]"
        ERROR_MSG_SUFFIX = (
            "Your changes have NOT been applied. Please fix your edit command and try again.\n"
            "You either need to 1) Open the correct file and try again or 2) Specify the correct line number arguments.\n"
            "DO NOT re-run the same failed edit command. Running it again will lead to the same error."
        )
        if not self._is_valid_filename(file_name.name):
            raise FileNotFoundError("Invalid file name.")
        if not self._is_valid_path(file_name):
            raise FileNotFoundError("Invalid path or file name.")
        if not self._create_paths(file_name):
            raise PermissionError("Could not access or create directories.")
        if not self._file_exists(file_name):
            raise FileNotFoundError(f"File {file_name} not found.")
        if is_insert and is_append:
            raise ValueError("Cannot insert and append at the same time.")

        content = str(content or "")
        first_error_line = None
        backup_content: Optional[str] = None
        src_abs_path = file_name

        try:
            original_lint_error = None
            if self.enable_auto_lint:
                original_lint_error, _ = self._lint_file(file_name)

            original_content = self._read_file(file_name)
            lines = original_content.splitlines(True)
            backup_content = original_content
            if is_append:
                content, n_added_lines = self._append_impl(lines, content)
            elif is_insert:
                try:
                    content, n_added_lines = self._insert_impl(lines, start, content)
                except LineNumberError as e:
                    return f"{ERROR_MSG}\n{e}\n{ERROR_MSG_SUFFIX}\n"
            else:
                try:
                    content, n_added_lines = self._edit_impl(lines, start, end, content)
                except LineNumberError as e:
                    return f"{ERROR_MSG}\n{e}\n{ERROR_MSG_SUFFIX}\n"
            if not content.endswith("\n"):
                content += "\n"
            self._write_file(src_abs_path, content)

            if self.enable_auto_lint:
                lint_error, first_error_line = self._lint_file(file_name)

                def extract_last_part(line):
                    parts = line.split(":")
                    return parts[-1].strip() if len(parts) > 1 else line.strip()

                def subtract_strings(str1, str2) -> str:
                    lines1 = str1.splitlines()
                    lines2 = str2.splitlines()
                    last_parts1 = [extract_last_part(line) for line in lines1]
                    remaining_lines = [line for line in lines2 if extract_last_part(line) not in last_parts1]
                    return "\n".join(remaining_lines)

                if original_lint_error and lint_error:
                    lint_error = subtract_strings(original_lint_error, lint_error)
                    if lint_error == "":
                        lint_error = None
                        first_error_line = None

                if lint_error is not None:
                    if is_append:
                        show_line = len(lines)
                    elif start is not None and end is not None:
                        show_line = int((start + end) / 2)
                    else:
                        raise ValueError("Invalid state. This should never happen.")

                    guidance_message = self._get_indentation_info(content, start or len(lines))
                    guidance_message += (
                        "You either need to 1) Specify the correct start/end line arguments or 2) Correct your edit code.\n"
                        "DO NOT re-run the same failed edit command. Running it again will lead to the same error."
                    )
                    backup_print_path = Path(f"/tmp/ed_backup_{os.getpid()}_{file_name.name}")
                    self._write_file(backup_print_path, backup_content)
                    lint_error_info = ERROR_GUIDANCE.format(
                        linter_error_msg=LINTER_ERROR_MSG + lint_error,
                        window_after_applied=self._print_window(file_name, show_line, n_added_lines + 20),
                        window_before_applied=self._print_window(backup_print_path, show_line, n_added_lines + 20),
                        guidance_message=guidance_message,
                    ).strip()
                    self._write_file(src_abs_path, backup_content)
                    return lint_error_info

        except FileNotFoundError as e:
            return f"File not found: {e}\n"
        except IOError as e:
            return f"An error occurred while handling the file: {e}\n"
        except ValueError as e:
            return f"Invalid input: {e}\n"
        except Exception as e:
            guidance_message = self._get_indentation_info(content, start or len(lines))
            guidance_message += (
                "You either need to 1) Specify the correct start/end line arguments or 2) Enlarge the range of original code.\n"
                "DO NOT re-run the same failed edit command. Running it again will lead to the same error."
            )
            backup_print_path = Path(f"/tmp/ed_backup_{os.getpid()}_{file_name.name}")
            if backup_content is not None:
                self._write_file(backup_print_path, backup_content)
            error_info = ERROR_GUIDANCE.format(
                linter_error_msg=LINTER_ERROR_MSG + str(e),
                window_after_applied=self._print_window(file_name, start or len(lines), 100),
                window_before_applied=self._print_window(backup_print_path, start or len(lines), 100),
                guidance_message=guidance_message,
            ).strip()
            if backup_content is not None:
                self._write_file(src_abs_path, backup_content)
            raise Exception(f"{error_info}") from e

        n_total_lines = max(1, len(self._read_file(file_name).splitlines()))
        if first_error_line is not None and int(first_error_line) > 0:
            self.current_line = first_error_line
        else:
            if is_append:
                self.current_line = max(1, len(lines))
            else:
                self.current_line = start or n_total_lines or 1
        success_edit_info = SUCCESS_EDIT_INFO.format(
            file_name=file_name,
            n_total_lines=n_total_lines,
            window_after_applied=self._print_window(file_name, self.current_line, self.window),
            line_number=self.current_line,
        ).strip()
        return success_edit_info

    def edit_file_by_replace(
        self,
        file_name: str,
        first_replaced_line_number: int,
        first_replaced_line_content: str,
        last_replaced_line_number: int,
        last_replaced_line_content: str,
        new_content: str,
    ) -> str:
        """Replace lines from first_replaced_line_number to last_replaced_line_number (inclusive) with new_content."""
        file_name = self._try_fix_path(file_name)
        mismatch_error = ""
        content = self._read_file(file_name)
        if not content.endswith("\n"):
            content += "\n"
        lines = content.splitlines(True)
        total_lines = len(lines)
        check_list = [
            ("first", first_replaced_line_number, first_replaced_line_content),
            ("last", last_replaced_line_number, last_replaced_line_content),
        ]
        for position, line_number, line_content in check_list:
            if line_number > len(lines) or lines[line_number - 1].rstrip() != line_content:
                start = max(1, line_number - 3)
                end = min(total_lines, line_number + 3)
                context = "\n".join(
                    [
                        f'The {cur_line_number:03d} line is "{lines[cur_line_number-1].rstrip()}"'
                        for cur_line_number in range(start, end + 1)
                    ]
                )
                mismatch_error += LINE_NUMBER_AND_CONTENT_MISMATCH.format(
                    position=position,
                    line_number=line_number,
                    true_content=lines[line_number - 1].rstrip()
                    if line_number - 1 < len(lines)
                    else "OUT OF FILE RANGE!",
                    fake_content=line_content.replace("\n", "\\n"),
                    context=context.strip(),
                )
        if mismatch_error:
            raise ValueError(mismatch_error)
        ret_str = self._edit_file_impl(
            file_name,
            start=first_replaced_line_number,
            end=last_replaced_line_number,
            content=new_content,
        )
        self.resource.report(file_name, "path")
        return ret_str

    def _edit_file_by_replace(self, file_name: str, to_replace: str, new_content: str) -> str:
        """Search for `to_replace` in the file and replace it with `new_content`."""
        if to_replace == new_content:
            raise ValueError("`to_replace` and `new_content` must be different.")
        file_name = self._try_fix_path(file_name)
        file_content = self._read_file(file_name)
        if to_replace.strip() == "":
            if file_content.strip() == "":
                raise ValueError(f"The file '{file_name}' is empty. Please use the append method to add content.")
            raise ValueError("`to_replace` must not be empty.")
        if file_content.count(to_replace) > 1:
            raise ValueError(
                "`to_replace` appears more than once, please include enough lines to make code in `to_replace` unique."
            )
        start = file_content.find(to_replace)
        if start != -1:
            start_line_number = file_content[:start].count("\n") + 1
            end_line_number = start_line_number + len(to_replace.splitlines()) - 1
        else:
            def _fuzzy_transform(s: str) -> str:
                return re.sub(r"[^\S\n]+", "", s)

            to_replace_fuzzy = _fuzzy_transform(to_replace)
            file_content_fuzzy = _fuzzy_transform(file_content)
            start = file_content_fuzzy.find(to_replace_fuzzy)
            if start == -1:
                return f"[No exact match found in {file_name} for\n```\n{to_replace}\n```\n]"
            start_line_number = file_content_fuzzy[:start].count("\n") + 1
            end_line_number = start_line_number + len(to_replace.splitlines()) - 1

        ret_str = self._edit_file_impl(
            file_name,
            start=start_line_number,
            end=end_line_number,
            content=new_content,
            is_insert=False,
        )
        self.resource.report(file_name, "path")
        return ret_str

    def insert_content_at_line(self, file_name: str, line_number: int, insert_content: str) -> str:
        """Insert a block of code before the given line number in a file."""
        file_name = self._try_fix_path(file_name)
        ret_str = self._edit_file_impl(
            file_name,
            start=line_number,
            end=line_number,
            content=insert_content,
            is_insert=True,
            is_append=False,
        )
        self.resource.report(file_name, "path")
        return ret_str

    def append_file(self, file_name: str, content: str) -> str:
        """Append content to the given file."""
        file_name = self._try_fix_path(file_name)
        ret_str = self._edit_file_impl(
            file_name,
            start=None,
            end=None,
            content=content,
            is_insert=False,
            is_append=True,
        )
        self.resource.report(file_name, "path")
        return ret_str

    # ------------------------------------------------------------------
    # search / find (docker grep / find based)
    # ------------------------------------------------------------------
    def search_dir(self, search_term: str, dir_path: str = "./") -> str:
        """Searches for search_term in all files in dir."""
        import shlex
        dir_path = self._try_fix_path(dir_path)
        if not self._is_dir(dir_path):
            raise FileNotFoundError(f"Directory {dir_path} not found")
        matches = []
        cmd = (
            f"grep -rn --exclude='.*' --exclude-dir='.git' "
            f"{shlex.quote(search_term)} {shlex.quote(str(dir_path))} 2>/dev/null | head -200"
        )
        out = self._docker_exec(cmd)
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) >= 3:
                fp, ln, content = parts[0], parts[1], parts[2]
                try:
                    ln_num = int(ln)
                except ValueError:
                    continue
                matches.append((Path(fp), ln_num, content.strip()))
        if not matches:
            return f'No matches found for "{search_term}" in {dir_path}'
        num_matches = len(matches)
        num_files = len(set(match[0] for match in matches))
        if num_files > 100:
            return f'More than {num_files} files matched for "{search_term}" in {dir_path}. Please narrow your search.'
        res_list = [f'[Found {num_matches} matches for "{search_term}" in {dir_path}]']
        for file_path, line_num, line in matches:
            res_list.append(f"{file_path} (Line {line_num}): {line}")
        res_list.append(f'[End of matches for "{search_term}" in {dir_path}]')
        return "\n".join(res_list)

    def search_file(self, search_term: str, file_path: Optional[str] = None) -> str:
        """Searches for search_term in file. If file is not provided, searches in the current open file."""
        if file_path is None:
            file_path = self.current_file
        else:
            file_path = self._try_fix_path(file_path)
        if file_path is None:
            raise FileNotFoundError("No file specified or open. Use the open_file function first.")
        if not self._file_exists(file_path):
            raise FileNotFoundError(f"File {file_path} not found")
        matches = []
        content = self._read_file(file_path)
        for i, line in enumerate(content.splitlines(), 1):
            if search_term in line:
                matches.append((i, line.strip()))
        res_list = []
        if matches:
            res_list.append(f'[Found {len(matches)} matches for "{search_term}" in {file_path}]')
            for match in matches:
                res_list.append(f"Line {match[0]}: {match[1]}")
            res_list.append(f'[End of matches for "{search_term}" in {file_path}]')
        else:
            res_list.append(f'[No matches found for "{search_term}" in {file_path}]')
        extra = {"type": "search", "symbol": search_term, "lines": [i[0] - 1 for i in matches]} if matches else None
        self.resource.report(file_path, "path", extra=extra)
        return "\n".join(res_list)

    def find_file(self, file_name: str, dir_path: str = "./") -> str:
        """Finds all files with the given name in the specified directory."""
        import shlex
        file_name = self._try_fix_path(file_name)
        dir_path = self._try_fix_path(dir_path)
        if not self._is_dir(dir_path):
            raise FileNotFoundError(f"Directory {dir_path} not found")
        name_pattern = Path(str(file_name)).name
        out = self._docker_exec(
            f"find {shlex.quote(str(dir_path))} -name '*{name_pattern}*' -not -path '*/.git/*' 2>/dev/null | head -100"
        )
        matches = [Path(line) for line in out.splitlines() if line]
        res_list = []
        if matches:
            res_list.append(f'[Found {len(matches)} matches for "{file_name}" in {dir_path}]')
            for match in matches:
                res_list.append(f"{match}")
            res_list.append(f'[End of matches for "{file_name}" in {dir_path}]')
        else:
            res_list.append(f'[No matches found for "{file_name}" in {dir_path}]')
        return "\n".join(res_list)

    @staticmethod
    async def similarity_search(query: str, path: Union[str, Path]) -> List[str]:
        """Given a filename or a pathname, performs a similarity search for a given query across the specified file or path."""
        try:
            from metagpt.tools.libs.index_repo import IndexRepo
            return await IndexRepo.cross_repo_search(query=query, file_or_path=path)
        except ImportError:
            raise ImportError("To use the similarity search, you need to install the RAG module.")

    @staticmethod
    def is_large_file(content: str, mix_token_count: int = 0) -> bool:
        encoding = tiktoken.get_encoding("cl100k_base")
        token_count = len(encoding.encode(content))
        mix_token_count = mix_token_count or DEFAULT_MIN_TOKEN_COUNT
        return token_count >= mix_token_count
