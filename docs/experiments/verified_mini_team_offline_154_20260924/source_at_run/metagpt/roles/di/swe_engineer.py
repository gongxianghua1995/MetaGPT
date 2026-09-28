"""SWEBenchEngineer: SWEAgent with a dockerized persistent shell.

Inherits SWEAgent unchanged — same NEXT_STEP_TEMPLATE prompt, same Bash.run
single-tool interface, same MINIMAL_EXAMPLE, same run_eval/output_diff eval
interface. The ONLY change is terminal=DockerBash, which maintains a
persistent `docker exec -i bash` subprocess inside the per-instance container.
This persistence is required for SWE-agent's open/goto/edit/submit/state
functions, which rely on shell environment variables ($CURRENT_FILE, etc.)
that must survive across commands.

In MAS mode (mas_mode=True), when the agent finishes (todo=None), it publishes
a CodeReviewRequest to the Reviewer (routed through TeamLeader by MGXEnv)
instead of just stopping.
"""
import json
import re
from typing import ClassVar, Optional  # Python 3.9 compatibility (no `str | None`)

from pydantic import Field
from metagpt.roles.di import swe_protocol
from metagpt.roles.di.swe_protocol import SWECaseState

from metagpt.actions.di.run_command import RunCommand
from metagpt.actions.di.swe_review import CodeReviewRequest
from metagpt.logs import logger
from metagpt.roles.di.swe_agent import SWEAgent
from metagpt.tools.libs.docker_edit import DockerEdit
from metagpt.schema import AIMessage, Message, UserMessage
from metagpt.tools.libs.docker_bash import DockerBash

# Native function-calling tool schema (mirrors EvoMAS/mini-swe-agent's BASH_TOOL).
# The model fills only {"command": "..."}; the API layer handles all JSON
# escaping that RoleZero's JSON-in-content approach forces the model to do by
# hand — the root cause of weak-model edit failure on multi-line patches
# (smoke21/23: 0 edits on django-11138 where EvoMAS succeeds with the same model).
NATIVE_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command in the repository",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                }
            },
            "required": ["command"],
        },
    },
}

EDIT_NUDGE_THRESHOLD = 5
# Empty-response circuit breaker: after this many CONSECUTIVE skipped rounds
# the model is in a permanent empty state (context poisoning / provider
# degradation) — verify_skipe rerun burned the full 60-min wall clock on 15
# cases that never issued a single command. Breaker = one slim-prompt retry,
# then force-submit whatever is on disk.
EMPTY_BREAKER_LIMIT = 5
# Cleaned-up version of MINIMAL_EXAMPLE (metagpt/prompts/di/swe_agent.py):
# same locate -> edit(heredoc) -> submit skeleton, but /testbed-rooted and
# stripped of Browser.goto, git push, and pull-request steps that are absent
# or banned in the dockerized eval. The `sed -i` example is the interface the
# weak model actually succeeds with (EvoMAS evidence: 25 sed -i, 0 heredoc
# on the same model).
SANITIZED_EXAMPLE = """
## Example of an actions trajectory
User Requirement and Issue: Fix the bug in the repo. You DO NOT need to run or modify existing test case files.
NOTE: this example uses /testbed for brevity — if the task states the repository is at another path (e.g. /app), use THAT path exactly in every command; the two refer to the same repo. Always prefix commands with `cd <repo_path> &&` as shown.

### Locate the issue:
Thought: I need to find the relevant code in the repo at /testbed.
{
    "command_name": "Bash.run",
    "args": {
        "cmd": "cd /testbed && grep -rn 'the_function_name' --include=*.py | head -20"
    }
}
->
Thought: Open the file around the matching lines to read context.
{
    "command_name": "Bash.run",
    "args": {
        "cmd": "open /testbed/path/to/file.py"
    }
}
->

### Fix the bug:
Thought: Fix it with a targeted in-place edit.
{
    "command_name": "Bash.run",
    "args": {
        "cmd": "cd /testbed && sed -i 's/wrong_expression/fixed_expression/' path/to/file.py"
    }
}
->
Thought: Alternative: an exact-text replacement via DockerEdit.replace.
{
    "command_name": "DockerEdit.replace",
    "args": {
        "file": "/testbed/path/to/file.py",
        "old": "    return wrong_expression",
        "new": "    return fixed_expression"
    }
}
->
Thought: When the fix needs COORDINATED edits across 2+ files or 2+ sites,
use DockerEdit.multi_replace in ONE call instead of N separate replaces.
{
    "command_name": "DockerEdit.multi_replace",
    "args": {
        "files": ["/testbed/a.py", "/testbed/b.py", "/testbed/a.py"],
        "olds": ["    return old_a", "    def old_b():", "    old_a = 1"],
        "news": ["    return new_a", "    def new_b():", "    new_a = 2"]
    }
}
->
Thought: Verify the edit landed, then submit.
{
    "command_name": "Bash.run",
    "args": {
        "cmd": "cd /testbed && git diff"
    }
}
->
{
    "command_name": "Bash.run",
    "args": {
        "cmd": "submit"
    }
}
->
Thought: All done.
{
    "command_name": "end"
}
"""
# Evolved from EvoMAS's step_logger.patch: prompt-level nudges are soft and
# weak models routinely ignore them (smoke10: 14 nudges, 0 responses). When
# editing stalls or JSON output collapses, stop burning rounds/tokens and
# hard-submit whatever is on disk — the eval scores the patch, not the journey.
# Raised from 15->25 (and EDIT_DEADLINE_ROUND 7->10 below): audit of the
# metagpt_pro_full2 run showed 60/63 ansible and 55/60 openlibrary instances
# hit this trigger while the outer team n_round budget (30) still had 25+
# rounds left and Alex's own max_react_loop (600) was nowhere near exhausted
# — i.e. this idle-edit gate, not the team round budget, was the real
# bottleneck behind empty patches. Giving Alex more exploration/retry room
# here (still far below max_react_loop) directly targets that.
FORCE_SUBMIT_IDLE = 25
MAX_JSON_FAIL_STREAK = 3
# Rounds granted to act on Reviewer feedback after a re-arm (pro-run fix):
# a full FORCE_SUBMIT_IDLE window per revise cycle would blow the 60-min
# wall clock across the TL's 5 cycles; the feedback names exact files, so a
# shorter focused window suffices.
REARM_GRACE_ROUNDS = 12
# Commands allowed once the write phase starts: editing itself plus the
# SWE-agent locate commands it depends on (open/goto/state/find_file feed
# $CURRENT_FILE/$CURRENT_LINE used by edit), plus native write channels —
# `sed -i` proved to be the weak model's natural editing interface
# (EvoMAS: 25 sed -i edits, 0 heredoc edits on the SAME model), and `git diff`
# is the post-edit verification EvoMAS runs 200+ times. grep/cat -n style
# research is still blocked. Module-level because pydantic turns class-body
# `_NAME = ...` into ModelPrivateAttr (breaks .match).
_WRITE_ALLOWED = re.compile(
    r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?"
    r"(edit|submit|open|goto|state|find_file|sed\s+-i|git\s+diff)\b"
)
# An "edit happened" signal covers both the SWE-agent edit command and native
# bash writes (sed -i) — the unlock-on-success logic must fire for
# whichever interface the model actually uses.
_EDIT_CMD = re.compile(
    r"^\s*(?:cd\s+/(?:testbed|app)(?:/[^\s;&]+)?\s*&&\s*)?(edit\b|sed\s+-i|python3?\b|apply_patch\b|perl\s+-pi|(?:cat|tee)\s+[^\n]*[>])"
)
# smoke45 fix: `_EDIT_CMD` alone counted READ commands as edits —
# `python3 -c "print(open('f.py').read())"` and `python --version` both
# matched `python3?\b`, inflating _edits_count to a fake 2, which made the
# deadline-gate condition `edits_count == 0` permanently False. Alex then
# drifted 20 rounds to force-submit with an EMPTY patch (smoke45 root cause).
# Fix: python3/python only counts as an edit when the command carries actual
# WRITE semantics; sed -i / edit / DockerEdit keep matching unconditionally.
_PY_WRITE_SEMANTICS = re.compile(
    r"open\s*\([^)]*['\"][wa]\+?['\"]"   # open(..., 'w'/'a')
    r"|\.write\s*\("                      # .write(
    r"|\.write_text\s*\("                 # .write_text(
    r"|DockerEdit"                        # semantic editor call
    r"|\bsed\s+-i"                        # sed -i inside python subprocess (shell string)
    r"|['\"]sed['\"],\s*['\"]-i['\"]"    # subprocess argv form: ['sed', '-i', ...]
)


def _looks_like_edit(cmd: str) -> bool:
    """True iff cmd is a REAL file-editing command.

    Used by the deadline gate and the edit bookkeeping (_act_native_toolcall)
    so that read-only `python3 -c "print(open(...).read())"` no longer fakes
    an edit (smoke45 regression).
    """
    if not cmd:
        return False
    # This is only a legacy-path progress hint.  Never rely on a regexp
    # capture-group index here: a regex refactor must not turn classification
    # of a harmless command into a case-ending exception.
    if not _EDIT_CMD.match(cmd):
        return False
    body = re.sub(r"^\s*(?:cd\s+/(?:testbed|app)(?:/[^\s;&]+)?\s*&&\s*)?", "", cmd)
    if re.match(r"(?:edit\b|sed\s+-i|apply_patch\b|perl\s+-pi|(?:cat|tee)\s+[^\n]*[>])", body):
        return True
    # python/python3 branch: require write semantics
    return bool(_PY_WRITE_SEMANTICS.search(cmd))


def _is_python3_write(cmd: str) -> bool:
    """True iff cmd is a python3/python edit attempt (write semantics).

    We BLOCK these in _gated_bash_run because multi-line `python3 -c "..."`
    scripts are unreliable for weak models: the LLM truncates the script
    (unterminated string → shell syntax error), str.replace() doesn't match
    (no-op), or the relative path is wrong. The result is a silent
    `git diff`=empty and an empty patch. `sed -i` and DockerEdit.replace
    are far more reliable and are the only edit channels we now allow.
    """
    if not cmd:
        return False
    if not _EDIT_CMD.match(cmd):
        return False
    body = re.sub(r"^\s*(?:cd\s+/(?:testbed|app)(?:/[^\s;&]+)?\s*&&\s*)?", "", cmd)
    if not re.match(r"python3?\b", body):
        return False  # sed -i / edit are allowed
    # python/python3 branch: block if it has write semantics
    # Allow heredoc (python3 - <<'EOF') — reliable, no truncation risk
    if _HEREDOC_RE.search(cmd):
        return False
    return bool(_PY_WRITE_SEMANTICS.search(cmd))


# D2 fix helpers: working-tree change signal. `git status --porcelain` alone
# is NOT enough: a SECOND edit to an already-modified file leaves the
# porcelain output identical (" M ops.py" both times) and would be misread as
# a no-op — breaking the iterate-until-pass loop. Append a content hash of the
# full diff so same-file re-edits are detected. Untracked files still come
# from the porcelain part. Bytecode noise is filtered by _clean_status.
_GIT_FAIL = re.compile(r"fatal|not a git repository", re.I)
_STATUS_CMD = "(git status --porcelain; git diff | sha1sum) 2>&1"

# Extract a plausible .py file path from a read command like
# `sed -n '1110,1160p' lib/matplotlib/contour.py` or
# `grep -n "foo" sympy/polys/polytools.py`
_READ_FILE_RE = re.compile(
    r"([\w./-]+\.py\b)"  # any path ending in .py
)


# ① iteration nudge: appended to the FIRST verified edit's observation.
# Repo-specific test runners differ; name the common ones explicitly.
_ITERATION_NUDGE = (
    "\n[EDIT VERIFIED] Your change is now in the working tree. Do NOT stop "
    "after a single edit — VERIFY it:\n"
    "  1. locate the test for the behavior you just edited:\n"
    "     grep -rn 'def test' tests/ | grep -i <keyword-of-edited-symbol> | head\n"
    "  2. run ONLY that file: python -m pytest <file> -x -q\n"
    "     (django: python -m pytest <file> -x -q  or  "
    "python tests/runtests.py <module>; sympy: python -m pytest <file> or bin/test <file>; "
    "Go repos: go test ./<pkg>/ -run <TestName>; JS/TS repos: yarn jest <file>)\n"
    "  3. if the test FAILS: read the traceback and EDIT AGAIN. Iterate until "
    "it passes — partial fixes are not acceptable."
)


def _clean_status(s: str) -> str:
    """Normalized porcelain output: drop blank lines and pyc noise."""
    return "\n".join(
        ln for ln in (s or "").splitlines()
        if ln.strip() and "__pycache__" not in ln and not ln.rstrip().endswith(".pyc")
    ).strip()
# P0-1.1: Archaeology anti-pattern detection. Two-pass check:
# 1. _ANTI_CMD matches the command shape (git log / show / blame / reflog)
# 2. _ANTI_FLAGS searches the full command string for archaeology flags
#    (HEAD~, --oneline, commit hashes, etc.).
# Weak models burn 80% of rounds on these instead of editing.
_ANTI_CMD = re.compile(
    # Accept any chain that starts with `cd /testbed &&` (or direct) and has
    # git log/show/blame/reflog before a dominant "write command" (sed -i,
    # python -c, DockerEdit call, submit, pytest runner) takes over the main
    # intent. "Prepended ls / echo / git status" are common Alex habits and
    # must NOT bypass the anti-pattern gate (the old strict `cd && git only`
    # pattern missed 1/22 of historical archaeology cmds).
    #
    # v2 (after smoke44 anti FN): previous middle alternative list was
    # single-token EXACT match for each prefix — `ls` (bare), `pwd` (bare),
    # etc. Therefore `ls -la; git log` failed because `-la;` was not part
    # of the expected [;&|]+ suffix (there was nothing between the cmd-name
    # and the suffix). Fix: each archetype (ls/pwd/echo/git status/git
    # diff/cat/sed -n/grep) now accepts [^;&|]* before the combinator — so
    # any flags/args are consumed greedily up to the separator.
    r"^\s*(cd\s+/(?:testbed|app)\s*[;&|]+\s*)*"
    r"((ls|pwd|echo|git\s+status|git\s+diff|cat|sed\s+-n|grep)"
    r"[^;&|]*\s*[;&|]+\s*)*"
    r"git\s+(log|show|blame|reflog)\b"
)
_ANTI_FLAGS = re.compile(
    r"(HEAD[~^]|(?<![\w-])--oneline|(?<![\w-])--stat|(?<![\w-])--pretty|(?<![\w-])--grep|"
    # -N (bare dash-number, max commits). Weak models often append
    # redirects before the true delimiter.
    r"(?<!\w)-[0-9]{1,2}(?=(?:\s*[012]?>\S*\s*|\s*\|\S*\s*)*"
    r"(?:\s|$|&|\)|;|,|\|))|"
    # Short flags followed by a numeric value: `-n 5`, `-n5`, `-A 3`, ...
    # The previous rule only matched the `-<DIGIT>` shorthand, not the
    # spelled-out `-n <DIGITS>` form Alex uses most.
    r"(?<!\w)-[ABCcdefGHIJKlLmnOPpqrsSTUuVvwxYy]\s*[0-9]{1,2}(?=\s|$|[;&|)])|"
    r"(?<!\w)-p(?!\w)|(?<!\w)-S(?!\w)|"
    # HEAD:FILE colon syntax (git show HEAD:setup.py). Weak models use
    # this for archaeology reads.
    r"HEAD[0-9~^]*:[A-Za-z_.]"
    r"|\b[a-f0-9]{7,40}\b)"
)


def _is_anti_pattern(cmd: str) -> bool:
    """Return True iff cmd is an archaeology anti-pattern command.

    v3 (smoke45): previous TWO-match gate (_ANTI_CMD + _ANTI_FLAGS) required
    archaeology FLAGS to be present before blocking. That caused FNs on:
        'ls -la; git log'          (plain git log, no flags)
        'pwd || git show HEAD'     (HEAD is a plain ref, no ~/^)
    Weak models emit BARE `git log` / `git show HEAD` too (just as noisy as
    `--oneline` variants). Since Alex has NO legitimate reason to inspect
    commit history inside the docker image (the image has a single commit
    anyway; SWE-bench clones are shallow), any call to
    git log|show|blame|reflog is anti-pattern.
    """
    if not cmd:
        return False
    # Strip the shell-combinator TRAILER ONLY — pipeline pipes, redirects.
    # DO NOT strip `||` / `&&` / `;` because those are STATEMENT
    # combinators that the main regex already recognises. Missing this
    # distinction caused a FN on `pwd || git show HEAD`: the inner `|` of
    # the `||` operator matched the split regex, clipping the head to
    # just `"pwd "` (no git log/show phrase → no match).
    #
    # Single-pipe `| head -20` or `2>/dev/null` or `> /tmp/x` are trailers:
    head = re.split(
        r"(?<![|&;])\s*\|\s*(?![|&;])"  # single `|` flanked by no other &/|
        r"|\s+(?:1?>>?|2>>?|>>?)\s+\S",  # redirects 1/2/> / >>
        cmd, 1
    )[0]
    return bool(_ANTI_CMD.match(head))


# P0-1.0 (v2): grep usage guard.
# v1 was regex-based and had 4 bugs: (1) bundled short flags -rnE not
# recognised as containing -r; (2) --include=*.py placed AFTER the path
# (valid POSIX) was missed; (3) single-file path foo/bar.py triggered
# "looks like dir search" because its path had dir-name substrings;
# (4) backslash-escaped pipes in 'a\|b\|c' single-quote patterns (meant
# as literal-pipe in BRE, not as alternation) were parsed as broken
# alternation and their proposed fix pattern was truncated mid-quote.
#
# v2 uses shlex.split for a real POSIX argv parse, then inspects opts
# via getopt semantics, and checks paths with os.path.isfile/dir hints
# (falling back to the .py-extension heuristic when the real filesystem
# isn't accessible — the guard runs before docker exec, so we can't
# check /testbed paths directly, but .py suffix == single file holds
# well enough for the common Alex patterns).
import shlex
import os as _os

_GREP_DIRNAME_HINTS = ("django", "tests", "apps", "contrib", "db", "backends",
                       "models", "functions", ".", "/testbed", "/app", "/tmp")

# grep short flags that take a VALUE (not boolean). Per POSIX grep + GNU grep
# man page, the value-taking short options are:
#   -A NUM  -B NUM  -C NUM  -d ACTION  -D ACTION  -e PATTERN  -f FILE  -m NUM
# All other short flags (-r -R -n -E -F -G -P -i -w -x -v -c -L -l -o -q -s -h
# -H -U -a -b -I -T -Z -b) are BOOLEAN and do not consume the next arg.
# Misclassifying boolean flags as "taking value" broke parsing of:
#   grep -F 'a|b|c' file.py   (greedy took 'a|b|c' as -F value)
#   grep -rn 'pat' dir/       (treated 'n' as "takes value" glued "pat" etc.)
SHORT_FLAGS_THAT_TAKE_VALUE = frozenset("ABCdeDdfm")


def _path_is_likely_dir(p: str) -> bool:
    """True iff p is probably meant as a directory path (trigger -r).
    When we can answer from the string alone we prefer that:
      - empty string → stdin, treated as missing path downstream
      - trailing '/'         → dir
      - no '.' in last token → likely dir name (no extension = no file)
      - else, basename has one of the known django dirs (django, tests,
        backends, contrib, apps, models, functions, db) → dir
      - suffix .py            → single file
    """
    if not p:
        return False
    if p.endswith("/"):
        return True
    base = p.rsplit("/", 1)[-1]
    if "." not in base:
        return True  # no extension → treated as dir
    if base.endswith(".py"):
        return False
    if base in _GREP_DIRNAME_HINTS:
        return True
    # any parent token == known dir
    toks = p.rstrip("/").split("/")
    return any(t in _GREP_DIRNAME_HINTS for t in toks)


def _diagnose_grep(cmd: str) -> Optional[str]:
    """If cmd contains a broken grep invocation, return a 1-line block
    message telling the model exactly what to fix. None = OK."""
    if not cmd or "grep" not in cmd:
        return None
    # Strip leading "cd /testbed &&" (if any)
    body = re.sub(r"^\s*(cd\s+\S+\s*&&\s*)?", "", cmd, count=1)
    # --- Old bug: `re.split(r"[|;&>]")` broke QUOTED pattern content like
    # grep -nE 'a|b|c' file.py because it split on the '|' INSIDE the quotes.
    # Use a mini scan that obeys single/double quote / backslash escaping to
    # clip tail combinators (pipeline `| cmd`, `; cmd2`, `&& cmd2`, `|| cmd3`,
    # `> file`, `>> file`, `2> file`) while preserving quoted patterns.
    def _strip_tail_combinators(s: str) -> str:
        i = 0
        n = len(s)
        quote = None  # None / "'" / '"'
        while i < n:
            ch = s[i]
            if quote:
                if ch == "\\":
                    i += 2; continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch; i += 1; continue
            if ch == "\\":
                i += 2; continue
            # Detect unquoted shell combinators:
            t = s[i:]
            if t.startswith(">>") or t.startswith("2>") or t.startswith("1>"):
                return s[:i].rstrip()
            if ch in (">", ";", "&"):
                # `&&` and `||` also stop here (2-char combinator but it's fine)
                return s[:i].rstrip()
            if ch == "|":
                # pipe, NOT inside quotes (quote is None). Note: POSIX doesn't
                # have `||` combinator meaning inside cmd arg list only. But at
                # top-level cmd flow, || means OR; safe to truncate at any
                # unquoted `|` anyway since it's a shell combinator boundary.
                return s[:i].rstrip()
            i += 1
        return s.rstrip()
    head = _strip_tail_combinators(body).strip()
    if not head:
        return None
    try:
        argv = shlex.split(head, posix=True)
    except ValueError as exc:
        return (
            f"[BLOCKED grep syntax: unbalanced quotes ({exc})] "
            "Use: grep -rnE --include=*.py 'pattern' django/db/backends"
        )
    try:
        gi = argv.index("grep")
    except ValueError:
        # `foo && grep ...` → shlex produced something, just locate grep anywhere
        gi = None
        for i, tok in enumerate(argv):
            if tok == "grep":
                gi = i
                break
        if gi is None:
            return None
    argv = argv[gi:]  # argv[0] == "grep"
    # getopt-style parse, stop at first non-option that's not -short-val
    short_flags = ""
    long_opts = set()
    pattern: Optional[str] = None
    paths: list = []
    i = 1
    expect_val_for_long = None
    while i < len(argv):
        a = argv[i]
        if expect_val_for_long:
            long_opts.add(expect_val_for_long + "=" + a)
            expect_val_for_long = None
            i += 1
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            opt = a[2:]
            if "=" in opt:
                k, v = opt.split("=", 1)
                long_opts.add(k + "=" + v)
            else:
                # known value-taking long opts in grep: --include, --exclude,
                # --exclude-dir, --color, --context, --before, --after, ...
                if opt in {"include", "exclude", "exclude-dir", "color",
                           "context", "before-context", "after-context",
                           "max-count", "devices", "directories"}:
                    expect_val_for_long = opt
                else:
                    long_opts.add(opt)
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            # Bundled short flags: -rnE  →  'r', 'n', 'E'
            # Only a very small, explicit set of grep short flags takes a
            # value. Everything else is boolean (can be bundled). Getting
            # this wrong caused v2 to mis-parse "-F 'pattern|with|pipes'"
            # as "-F taking the next arg = pattern as file", which then
            # made the guard complain about missing path.
            s = a[1:]
            j = 0
            while j < len(s):
                ch = s[j]
                short_flags += ch
                if ch in SHORT_FLAGS_THAT_TAKE_VALUE:
                    # Value taking: if chars remain, rest of token is the
                    # value (e.g. -C2); else consume next argv as value.
                    if j + 1 < len(s):
                        break  # value glued (rest of this token)
                    i += 1   # consume separate argv as value
                    break
                j += 1
            i += 1
            continue
        # Positional: pattern, then paths
        if pattern is None:
            pattern = a
        else:
            paths.append(a)
        i += 1
    # Remaining (after --):
    for a in argv[i:]:
        if pattern is None:
            pattern = a
        else:
            paths.append(a)

    problems = []
    # Flag existence (bundled flags are already unpacked)
    has_R = "r" in short_flags or "R" in short_flags or "recursive" in long_opts
    has_E = "E" in short_flags or "extended-regexp" in long_opts
    has_F = "F" in short_flags or "fixed-strings" in long_opts
    has_include = any(k.startswith("include=") for k in long_opts)
    paths_stripped = [p for p in paths if p]
    any_looks_dir = any(_path_is_likely_dir(p) for p in paths_stripped)
    # --- actual rule checks ---
    if pattern is None:
        problems.append("missing pattern")
    if not paths_stripped:
        problems.append("missing path (grep waits on stdin)")
    if any_looks_dir:
        if not has_R:
            problems.append("missing -r for directory search")
        if not has_include:
            # Directory search → strong default .py filter
            problems.append("missing --include=*.py")
    # Alternation rule: ONLY when pattern contains a real pipe character
    # (not BRE-escaped \|). For Alex's patterns, he uses either:
    #   - BRE grep -n "a\|b\|c" (inside double quotes with backslashes)
    #     This is VALID BRE, no -E needed.
    #   - ERE grep -nE 'a|b|c'  (inside single quotes no backslashes)
    # So only flag if pattern has unescaped '|' AND no -E/-F.
    if pattern and ("|" in pattern):
        # Count unescaped pipes (an odd number of leading backslashes → escaped)
        unesc_pipe = False
        for k in range(len(pattern)):
            if pattern[k] == "|":
                bs = 0
                j = k - 1
                while j >= 0 and pattern[j] == "\\":
                    bs += 1
                    j -= 1
                if bs % 2 == 0:
                    unesc_pipe = True
                    break
        if unesc_pipe and not has_E and not has_F:
            problems.append("missing -E for 'a|b|c' alternation")

    if not problems:
        return None

    # Build the DIRECT FIX command, preserving the original pattern exactly.
    fix_parts = ["grep"]
    new_flags = set(short_flags)
    new_long = set(long_opts)
    if any_looks_dir or not paths_stripped:
        new_flags.add("r")
        new_flags.add("n")  # default on for human-readable line numbers
        if not has_include:
            new_long.add("include=*.py")
    if ("|" in (pattern or "")) and not has_E and not has_F:
        new_flags.add("E")
    # Reconstruct short flags string, keep 'r' 'n' 'E' together, then any others
    preferred = []
    for ch in "rnE":
        if ch in new_flags:
            preferred.append(ch)
            new_flags.discard(ch)
    rest_flags = sorted(new_flags)
    sf = "-" + "".join(preferred + rest_flags) if (preferred + rest_flags) else ""
    fix_parts.append(sf) if sf else None
    # Long opts: include=*.py first, then others (preserve originals from
    # the user if any)
    includes = sorted(k for k in new_long if k.startswith("include="))
    others   = sorted(k for k in new_long if not k.startswith("include="))
    for k in includes + others:
        fix_parts.append(f"--{k}")
    if pattern is None:
        fix_parts.append("'<KEYWORDS>'")
    else:
        # Use single-quote shell literal form, escaping embedded ' via '\''
        safe = pattern.replace("'", "'\\''")
        fix_parts.append(f"'{safe}'")
    # Paths: preserve as-is if present; otherwise default.
    if paths_stripped:
        for p in paths_stripped:
            fix_parts.append(p)
    else:
        fix_parts.append("django/db/backends")
        fix_parts.append("django/db/models/functions")
    fix = " ".join(fix_parts)
    # Cap length
    if len(fix) > 260:
        fix = fix[:257] + "..."
    return (
        f"[BLOCKED grep args: {', '.join(problems)}] "
        f"Use: {fix}"
    )
# P0-1.2: Hard edit deadline. After this many act rounds with 0 edits,
# only edit/submit commands are accepted.
# ROLLED BACK to baseline (smoke39 config) per user directive: the A/B/C
# guard stack (grep-args block + anti 1-hit block + post-grep enforcement)
# correlated with 0 patches across smoke43-45, while the smoke37-42 config
# (deadline gate only) produced smoke39's 1061-char patch. The deadline gate
# itself stays — it coexisted with patch production.
# Raised from 7->10 alongside FORCE_SUBMIT_IDLE (see above) to give more
# read-only exploration time before the write-only gate + idle clock kick in.
EDIT_DEADLINE_ROUND = 10

# Commands that revert working-tree changes — blocked when _edits_count > 0
# to prevent the agent from undoing its own edits (Category A: 6/19 empty patches)
_REVERT_CMD = re.compile(
    r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?"
    r"(git\s+(checkout|stash|reset\s+--hard|restore)\b)"
)
# Read-only commands that should pass the deadline gate (so the agent can
# inspect code to craft an edit).  Without this, the agent hits a deadlock:
# can't read (blocked by deadline) → can't craft an edit → can't write.
_READ_CMD = re.compile(
    r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?"
    r"(sed\s+-n|grep\b|cat\s|head\s|tail\s|ls\s|find\s|wc\s|git\s+diff\b|git\s+status\b)"
)
# python3 -c read-only探查 (import/inspect/print/--version) — 放行，不算编辑
_PY_READ_RE = re.compile(
    r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?python3?\s+-c\s+"
)
# pytest / python -m pytest / python tests/runtests.py — 限次放行.
# Pro fix: the whitelist only knew Python runners, so on flipt (Go) and
# webclients (TS) every `go test` / `yarn test` was gated as generic research
# past the edit deadline — the agent could never verify on 2 of 4 domains.
_PYTEST_CMD = re.compile(
    r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?"
    r"(python3?\s+(-m\s+)?pytest\b|python3?\s+tests/runtests\.py\b|bin/test\b"
    r"|go\s+test\b|yarn\s+(run\s+)?(jest|test)\b|npx\s+jest\b"
    r"|npm\s+(run\s+)?test\b|pnpm\s+(run\s+)?test\b)"
)
# heredoc syntax: python3 - <<'EOF' — 可靠，无截断风险
_HEREDOC_RE = re.compile(r"<<\s*['\"]?EOF['\"]?")

# Source-file paths mentioned in Reviewer feedback / interface specs —
# used to build concrete edit targets for the re-arm brief and the
# interface-coverage submit check.
_FEEDBACK_PATH_RE = re.compile(
    r"\b((?:[\w.-]+/)+[\w.-]+\.(?:py|go|ts|tsx|js|jsx|rb|java|c|h|cpp|yml|yaml))\b"
)

# smoke48 anchor-enhancement: after the deadline, BLOCK messages become
# constructive — they echo the code evidence ALEX HIMSELF collected via
# grep/sed -n (never gold-patch semantics, so this stays within the
# "anchors only" constraint) plus a sed skeleton he can fill in.
_EVIDENCE_CMD = re.compile(r"(^|;|&&|\|\||\s)(grep|sed\s+-n)\b")

SWE_ENGINEER_INSTRUCTION = """You are the Engineer fixing one SWE-bench issue in the checked-out repository.
Use the issue, requirements, interface, and command observations as your source of truth.
Work in a short evidence loop: locate the relevant implementation, read focused context,
form a concrete hypothesis, edit the source, run a focused test or reproduction, inspect
the failure if it fails, and iterate. Do not modify tests to hide a failure. Do not use
git history as a substitute for understanding the current checkout. You have one tool:
call bash with a single concrete command. Use ordinary shell edits and finish by calling
bash with `submit` after the working tree contains the best tested patch. Do not call
unavailable tools, ask a human, or claim success from a diff alone."""


class SWEBenchEngineer(SWEAgent):
    """SWEAgent with a dockerized persistent terminal. Everything else upstream."""

    name: str = "Alex"
    instruction: str = SWE_ENGINEER_INSTRUCTION
    # exclude=True: Team.serialize / serialize_decorator dumps the whole role
    # graph; a bare DockerBash field makes model_dump() raise
    # "Unable to serialize unknown type" and masks the real exception.
    terminal: DockerBash = Field(default=None, exclude=True)
    # Semantic replacement tool (containerized Editor.edit_file_by_replace
    # equivalent). Weak models cannot reliably format heredoc-based `edit`
    # commands inside JSON; they demonstrably prefer this interface (smoke8
    # hallucinated exactly this signature).
    docker_edit: DockerEdit = Field(default=None, exclude=True)
    mas_mode: bool = False
    case_state: Optional[SWECaseState] = Field(default=None, exclude=True)
    review_reserve_seconds: float = 120
    max_native_steps: int = 80
    # Keep action history below the range where the compatible provider
    # routinely times out; commands and full evidence remain in trace files.
    native_context_chars: int = 24000
    native_deadline: Optional[float] = None
    _native_turns: list = []
    _last_rearmed_submission: int = 0
    task_requirements: str = ""
    interface_files: list[str] = Field(default_factory=list)
    # When True, bypass RoleZero's JSON-in-content command parsing and use
    # OpenAI-native function-calling (tools=[NATIVE_BASH_TOOL]). The model
    # fills {"command": "..."} via the API's tool_calls field, avoiding the
    # hand-escaped JSON that breaks weak models on multi-line edits. This
    # mirrors EvoMAS/mini-swe-agent's approach (BASH_TOOL + litellm).
    use_native_toolcall: bool = True
    memory_k: int = 20
    # Override parent's 40 so the Engineer has enough budget to finish MySQL +
    # SQLite + Oracle backends and still reach the MAS handoff (CodeReviewRequest)
    # before loop exhaustion.
    max_react_loop: int = 600
    # Minimal-tool policy for the dockerized eval: only container-safe tools.
    # Browser is useless inside a headless container; Editor is actively harmful
    # (its working_dir points at HOST files, not the container's /testbed).
    tools: list[str] = ["Bash"]
    _act_count: int = 0
    # Empty-response skip: weak models (Deepseek-V4-Flash-0731) emit 9-22
    # bare empty replies per failing case — each burns a full react round AND
    # pollutes memory with "(empty response)" + "You MUST issue..." pairs.
    # Track consecutive empty rounds so we can: (a) skip counting them in
    # _act_count / force-submit idle, and (b) inject a targeted warning into
    # the next prompt instead of cluttering memory.
    _consecutive_empty: int = 0
    # Set by the empty-response breaker in _think; consumed in _act which
    # routes to _force_submit (same message flow as the idle trigger).
    _empty_breaker: bool = False
    # Editing discipline tracks WHEN the last edit happened, not whether one
    # ever occurred. The original one-shot boolean (`_has_edited`) silenced the
    # nudge forever after the first edit — exactly when a multi-backend fix
    # needs sustained pressure. smoke9 lost ~30 rounds to post-first-edit
    # complacency (endless git-log archaeology, zero second edits).
    _edits_count: int = 0
    _round_of_last_edit: int = 0
    _json_fail_streak: int = 0
    _force_done: bool = False
    # Re-arm bookkeeping (pro-run fix): _idle_floor shifts the idle-clock
    # origin forward on re-arm so a re-armed Engineer is not instantly
    # re-force-submitted by the stale idle counter; _rearmed_msg_ids ensures
    # each CodeReviewFeedback message re-arms at most once (otherwise the
    # same stale feedback in recent memory would re-arm in a loop).
    _idle_floor: int = 0
    _rearmed_msg_ids: Optional[set] = None
    # Interface-coverage submit check (pro2 fix): SWE-bench Pro's `interface`
    # section names the exact files the hidden tests exercise. If the first
    # submit's diff touches NONE of them, the patch is almost certainly
    # off-target (pro2: median patch = 1 small file vs multi-file golds).
    # Block that submit ONCE with the concrete file list.
    _iface_block_used: bool = False
    # Phased tool whitelist: after EXPLORE_BUDGET rounds without an edit, the
    # smoke18 proved gate hard-block triggers avoidance: model outputs blank
    # JSON when forced to edit (3 consecutive failures at gate trigger). EvoMAS
    # uses prompt-level "step 10 edit immediately" with FULL tool freedom and
    # the same model edits naturally. Disable gate: keep _gated_bash_run as a
    # pass-through, let prompt constitution + sed -i example do the steering.
    explore_budget: int = 999
    _unlocked: bool = False
    _anti_violations: int = 0  # P0-1.1: anti-pattern command counter
    # smoke48 anchor-enhancement: last successful grep/sed -n output, fed
    # back into deadline BLOCK messages as Alex's own code evidence.
    _evidence_cmd: str = ""
    _evidence_out: str = ""
    # D2 fix (post-batch audit): 24/60 failed cases emitted 17+ write
    # commands that SILENTLY changed nothing (bad OLD_TEXT, wrong path) yet
    # were counted as edits by _looks_like_edit alone — inflating
    # _edits_count, unlocking the gate, and force-submitting an EMPTY diff.
    # Write-post verification: snapshot `git status --porcelain` around each
    # bash write; only a REAL working-tree change counts as an edit.
    _diff_after_last_write: Optional[str] = None
    _last_write_counted: bool = False
    # Deadline-gate read-pass: after EDIT_DEADLINE_ROUND with 0 edits, allow
    # read commands (sed -n, grep, cat, ls) to pass but inject an edit hint
    # every MAX_DEADLINE_READ_PASSES passes. Prevents the read/write deadlock
    # where the agent can't read code to craft an edit AND can't edit.
    _deadline_read_passes: int = 0
    MAX_DEADLINE_READ_PASSES: ClassVar[int] = 3
    _deadline_test_passes: int = 0
    MAX_DEADLINE_TEST_PASSES: ClassVar[int] = 2

    def _retrieve_experience(self) -> str:
        """Sanitized demonstration: MINIMAL_EXAMPLE (inherited via SWEAgent)
        teaches the right edit mechanics but is laced with host landmines —
        `/workspace/MetaGPT` paths that don't exist in the container, a
        Browser.goto opener whose execution mapping was removed, and a
        git-push/pull-request finale that is banned in eval mode. A weak model
        imitates the demo verbatim, fails on step one, and falls back to
        grep loops. Align every path with /testbed and keep only the generic
        locate -> edit -> submit skeleton (no instance hints involved).
        """
        return SANITIZED_EXAMPLE

    def _update_tool_execution(self):
        self.tool_execution_map.update(
            {
                "Bash.run": self._gated_bash_run,
                "DockerEdit.replace": self._docker_edit_replace,
                "DockerEdit.multi_replace": self._docker_edit_multi_replace,
                "git_create_pull": self._git_create_pull_stub,
            }
        )

    async def _docker_edit_replace(self, file: str, old: str, new: str) -> str:
        """DockerEdit.replace with edit-discipline bookkeeping: a successful
        replacement unlocks the full toolset (write phase over) and refreshes
        the sustained-edit nudge timers, same as a successful `edit` should.
        """
        result = await self.docker_edit.replace(file=file, old=old, new=new)
        if result.startswith("REPLACE_OK"):
            self._unlocked = True
            self._edits_count += 1
            self._round_of_last_edit = self._act_count
        return result

    async def _docker_edit_multi_replace(self, files: list, olds: list, news: list) -> str:
        """DockerEdit.multi_replace with the same bookkeeping as replace. A
        successful batch (all N edits applied) counts as one editing pass but
        records N edits for the sustained-edit nudge.
        """
        result = await self.docker_edit.multi_replace(files=files, olds=olds, news=news)
        if result.startswith("MULTI_REPLACE_OK"):
            self._unlocked = True
            self._edits_count += len(files)
            self._round_of_last_edit = self._act_count
        return result
        # RoleZero.set_tool_execution unconditionally registers every Editor.*
        # and Browser.* method in the execution map regardless of the tools
        # whitelist. A weak model can therefore emit commands it never saw a
        # schema for (smoke8: hallucinated Editor.edit_file_by_replace with
        # wrong kwargs). Worse, had the signature matched, Editor would have
        # edited HOST files under DEFAULT_WORKSPACE_ROOT instead of /testbed.
        # Remove them: stray commands now fall through to "Command not found",
        # which steers the model back to Bash.run.
        stray = [
            k
            for k in self.tool_execution_map
            if k.startswith("Editor.") or k.startswith("Browser.")
        ]
        for k in stray:
            del self.tool_execution_map[k]

    # Commands allowed once the write phase starts: editing itself plus the
    # SWE-agent locate commands it depends on (open/goto/state/find_file feed
    # $CURRENT_FILE/$CURRENT_LINE used by edit). Everything research-only —
    # grep/cat/sed/git-log archaeology — is blocked.

    def _maybe_rearm_on_feedback(self) -> None:
        """Clear _force_done when the Reviewer sent NEW CodeReviewFeedback.

        BUG FIX (pro-run trajectory audit): this check used to live only in
        _think, but _react early-returns on _force_done BEFORE _think ever
        runs — the re-arm was dead code. Across all pro_* logs the counts
        were 391x "force-done, staying silent" vs 0x "re-arming Engineer",
        i.e. the TL's revise cycles always forwarded feedback to a
        permanently mute Engineer. Calling this from _react (before the
        early return) makes rejection feedback actionable again.

        Guards: each feedback message re-arms at most once (tracked by id),
        and the idle clock is pushed forward via _idle_floor so the engineer
        gets REARM_GRACE_ROUNDS rounds to act instead of being instantly
        re-force-submitted.
        """
        if self.case_state is not None:
            state = self.case_state
            if (self._force_done and state.phase == "editing"
                    and state.submission_id > self._last_rearmed_submission):
                self._last_rearmed_submission = state.submission_id
                self._force_done = False
                self._consecutive_empty = 0
                self._silent = False
            return
        if not (self._force_done and self.mas_mode and self.rc.env):
            return
        if self._rearmed_msg_ids is None:
            self._rearmed_msg_ids = set()
        recent = self.get_memories()[-5:] if self.get_memories() else []
        for msg in recent:
            # MetaGPT stores cause_by as a string (class name), not a type
            # object — isinstance(msg.cause_by, type) is always False.
            cause_name = (
                msg.cause_by
                if isinstance(msg.cause_by, str)
                else getattr(msg.cause_by, "__name__", str(msg.cause_by))
            )
            if "CodeReviewFeedback" not in cause_name:
                continue
            msg_id = getattr(msg, "id", None) or id(msg)
            if msg_id in self._rearmed_msg_ids:
                continue
            self._rearmed_msg_ids.add(msg_id)
            self._force_done = False
            self._silent = False
            self._consecutive_empty = 0
            self._empty_breaker = False
            self._json_fail_streak = 0
            # Fresh (bounded) editing window: without this, idle_rounds is
            # already >= FORCE_SUBMIT_IDLE and the next _act would
            # force-submit again before any feedback-driven edit.
            self._idle_floor = self._act_count - (FORCE_SUBMIT_IDLE - REARM_GRACE_ROUNDS)
            logger.info("Reviewer feedback received — re-arming Engineer.")
            # Post-re-arm conversion fix (pro2 audit: 151 re-arms, only 39
            # produced a post-re-arm edit): the bare feedback prose leaves a
            # weak model exploring again. Inject a structured action brief:
            # the concrete files named in the feedback + a hard "edit first"
            # directive, so the very next command is a write, not a grep.
            fb_files = sorted(set(_FEEDBACK_PATH_RE.findall(msg.content or "")))[:6]
            files_hint = (
                "Files named in the feedback: " + ", ".join(fb_files) + "\n"
                if fb_files else ""
            )
            self.rc.memory.add(UserMessage(
                content=(
                    "[RE-ARMED AFTER REVIEW] The Reviewer REJECTED the current "
                    f"diff. You have {REARM_GRACE_ROUNDS} rounds to fix it.\n"
                    + files_hint +
                    "Read only the missing local context needed for a precise "
                    "edit, then fix the specific rejected behavior. Do not repeat "
                    "whole-file reads. Check every requirement, run a focused "
                    "test or reproduction, inspect `git diff`, then `submit`.\n"
                    + self.task_requirements
                ),
                cause_by=RunCommand,
            ))
            break

    async def _react(self) -> Message:
        """smoke28 fix: a force-done Engineer must NOT publish a
        'No actions taken yet' AIMessage — that gets routed to Mike,
        re-activating him and causing an infinite TL-explore loop.
        Return an empty AIMessage so _observe sees no news and the role
        goes idle without spamming the team bus.

        Pro-run fix: check for Reviewer feedback FIRST — the early return
        below used to fire before _think's re-arm logic could ever run,
        killing every revise cycle (see _maybe_rearm_on_feedback)."""
        self._maybe_rearm_on_feedback()
        if self._force_done:
            logger.debug(f"{self._setting}: force-done, staying silent.")
            return AIMessage(content="", sent_from=self.name, cause_by=RunCommand)
        return await super()._react()

    async def _gated_bash_run(self, cmd: str, **kwargs) -> str:
        """Bash.run behind the baseline (smoke39-config) gates.

        ROLLED BACK (smoke46): the A/B/C guard stack added after smoke42 —
        grep-args hard-block (A), anti-pattern 1-hit block (B), and the
        post-grep-BLOCK enforcement state machine (C) — correlated with
        0 patches in smoke43/44/45, versus smoke39's 1061-char patch on the
        pre-guard config. Per user directive the baseline is restored:

        1. Anti-pattern (2-strike): git log/show archaeology RUNS with a
           short warning banner on violations 1-2, and is BLOCKED from
           violation 3 onward. (The old 1-hit block produced escape
           behaviour: smoke44 logged 8 blocked archaeology attempts plus
           11 no-information `ls/git status` rounds.)
        2. Edit deadline: after EDIT_DEADLINE_ROUND rounds with 0 REAL
           edits only edit/submit commands pass. Real-edit detection now
           uses _looks_like_edit() so `python3 -c` READ commands no longer
           fake an edit count (the smoke45 root cause).
        3. Phased whitelist (explore_budget) — legacy pass-through gate.

        The grep-args guard (_diagnose_grep v2) is no longer invoked here;
        the function stays in the file (fully unit-tested) in case it is
        re-enabled as an advisory (non-blocking) check later.
        """
        raw = cmd or ""

        if (self.interface_files and not self._iface_block_used
                and re.fullmatch(r"\s*(?:cd\s+/(?:testbed|app)\s*&&\s*)?submit\s*;?\s*", raw)):
            changed = await self.terminal.run(
                cmd='(cd "$(git rev-parse --show-toplevel)" && '
                    'git diff HEAD --name-only && git ls-files --others --exclude-standard)'
            )
            files = set(changed.splitlines())
            if files and not _GIT_FAIL.search(changed) and not files.intersection(self.interface_files):
                self._iface_block_used = True
                return (
                    "[SCOPE CHECK] No changed file matches an explicit interface Path. "
                    "Review the required behavior before submitting. Interface files: "
                    + ", ".join(self.interface_files) + ". Changed files: "
                    + ", ".join(sorted(files)) + ". A fix elsewhere may be valid; "
                    "verify it with a focused test. You may submit again after checking."
                )

        # ---- Anti-pattern git archaeology: 2-strike, baseline policy -------
        if _is_anti_pattern(raw):
            self._anti_violations += 1
            if self._anti_violations <= 2:
                # Baseline behaviour (smoke37-42 era): run the command, then
                # append a SHORT warning (never the old 700-char banner).
                out = await self.terminal.run(cmd=cmd, **kwargs)
                logger.warning(
                    f"anti-pattern warning #{self._anti_violations} (executed): "
                    f"cmd[:80]={raw[:80]}"
                )
                return out + (
                    f"\n[anti-pattern #{self._anti_violations}] git archaeology "
                    f"is FUTURE history and misleads; 0-{self._edits_count} edits so far. "
                    "Edit NOW: sed -i or DockerEdit.replace."
                )
            logger.warning(
                f"anti-pattern violation #{self._anti_violations}: blocked archaeology cmd "
                f"(round {self._act_count}, edits={self._edits_count}). cmd[:80]={raw[:80]}"
            )
            return (
                f"[BLOCKED git archaeology #{self._anti_violations}] "
                f"git log/show commits are FUTURE and MISLEAD. "
                f"0 edits after {self._act_count} rounds. "
                f"EDIT NOW: sed -i 's/old/new/' target_file.py  "
                f"or DockerEdit.replace(file=target, old=exact, new=fixed)"
            )

        # ---- Block git checkout/stash/reset when edits exist (Category A fix) ----
        # 6/19 empty patches were caused by the agent reverting its own edits
        # via `git checkout`, `git stash`, or `git reset --hard` and never
        # re-applying them. Block these revert commands when _edits_count > 0.
        if _REVERT_CMD.match(raw) and self._edits_count > 0:
            logger.info(
                f"blocked revert cmd (edits={self._edits_count}): cmd[:80]={raw[:80]}"
            )
            return (
                "\n[BLOCKED] Do NOT run git checkout/stash/reset — "
                "you have ACTIVE edits that will be lost. "
                "If you need a clean test run, use `git diff` to verify "
                "your changes first, then `submit`. "
                "If the edit is wrong, fix it with another `sed -i`.\n"
            )

        # ---- Block python3 -c write attempts (unreliable for weak models) ----
        # Multi-line `python3 -c "..."` edits frequently fail silently: the LLM
        # truncates the script (unterminated string), the replace() doesn't
        # match, or the CWD/path is wrong — producing `git diff`=empty and an
        # empty patch. `sed -i` (single-line, easy to verify) and
        # DockerEdit.replace (dedicated tool with exact old/new) are far more
        # reliable. Block python3 writes and steer the model there.
        if _is_python3_write(raw):
            logger.info(
                f"blocked python3 write attempt (round {self._act_count}, "
                f"edits={self._edits_count}): cmd[:80]={raw[:80]}"
            )
            _file_hint = ""
            _mf = _READ_FILE_RE.search(raw)
            if _mf:
                _file_hint = _mf.group(1)
            if _file_hint:
                return (
                    "\n[BLOCKED] Do NOT use `python3 -c` for editing — it is "
                    "unreliable (truncated scripts / no-op replaces). Use ONE of:\n"
                    f"  1. sed -i 's/OLD_LINE/NEW_FIXED_LINE/' {_file_hint}\n"
                    f"  2. DockerEdit.replace(file='{_file_hint}', old='verbatim', new='fixed')\n"
                    "Copy OLD_TEXT verbatim from your last grep/sed -n output "
                    "(exact indentation).\n"
                )
            return (
                "\n[BLOCKED] Do NOT use `python3 -c` for editing — it is "
                "unreliable (truncated scripts / no-op replaces). Use ONE of:\n"
                "  1. sed -i 's/OLD_TEXT_EXACTLY/NEW_FIXED_TEXT/' <file>\n"
                "  2. DockerEdit.replace(file='<file>', old='verbatim', new='fixed')\n"
                "Copy OLD_TEXT verbatim from your last grep/sed -n output "
                "(exact indentation).\n"
            )

        # ---- Edit-deadline gate (revised: allow reads with hints) ----------
        past_deadline = (
            self._act_count > EDIT_DEADLINE_ROUND
            and self._edits_count == 0
        )
        if past_deadline and not _looks_like_edit(raw) and "DockerEdit" not in raw:
            is_submit = bool(
                re.match(r"^\s*(cd\s+/(?:testbed|app)\s*&&\s*)?submit\b", raw)
            )
            if not is_submit:
                # Allow read-only commands (sed -n, grep, cat, etc.) and
                # python3 -c read-only探查 to pass, but inject an edit hint
                # every MAX_DEADLINE_READ_PASSES times.
                # This prevents the read/write deadlock (Category D: 7/19 empty).
                is_py_read = (
                    _PY_READ_RE.match(raw) and not _is_python3_write(raw)
                )
                if _READ_CMD.match(raw) or is_py_read:
                    self._deadline_read_passes += 1
                    out = await self.terminal.run(cmd=cmd, **kwargs)
                    if self._deadline_read_passes >= self.MAX_DEADLINE_READ_PASSES:
                        self._deadline_read_passes = 0
                        # Extract file path from the read command
                        _blocked_file = ""
                        _mf = _READ_FILE_RE.search(raw)
                        if _mf:
                            _blocked_file = _mf.group(1)
                        if not _blocked_file and self._evidence_cmd:
                            _mf2 = _READ_FILE_RE.search(self._evidence_cmd)
                            if _mf2:
                                _blocked_file = _mf2.group(1)
                        _file_hint = _blocked_file or "<file>"
                        out = out + (
                            f"\n[EDIT HINT] Round {self._act_count}, 0 edits. "
                            f"You just read code — now EDIT it:\n"
                            f"  sed -i 's/OLD_LINE/NEW_FIXED_LINE/' {_file_hint}\n"
                            f"  DockerEdit.replace(file='{_file_hint}', old='verbatim', new='fixed')\n"
                        )
                    return out
                # Allow limited pytest runs (up to MAX_DEADLINE_TEST_PASSES)
                # so the model can confirm the bug before editing
                if _PYTEST_CMD.match(raw) and self._deadline_test_passes < self.MAX_DEADLINE_TEST_PASSES:
                    self._deadline_test_passes += 1
                    out = await self.terminal.run(cmd=cmd, **kwargs)
                    if self._deadline_test_passes >= self.MAX_DEADLINE_TEST_PASSES:
                        _blocked_file = ""
                        if self._evidence_cmd:
                            _mf = _READ_FILE_RE.search(self._evidence_cmd)
                            if _mf:
                                _blocked_file = _mf.group(1)
                        _file_hint = _blocked_file or "<file>"
                        out = out + (
                            f"\n[TEST LIMIT] You've run tests {self._deadline_test_passes}x "
                            f"without editing. EDIT NOW:\n"
                            f"  sed -i 's/OLD_LINE/NEW_FIXED_LINE/' {_file_hint}\n"
                            f"  DockerEdit.replace(file='{_file_hint}', old='verbatim', new='fixed')\n"
                        )
                    return out
                # Non-read, non-edit, non-submit commands are still blocked
                logger.info(
                    f"deadline gate (round {self._act_count} > "
                    f"{EDIT_DEADLINE_ROUND}, edits=0) blocked cmd[:80]={raw[:80]}"
                )
                # Extract file path from the blocked read command to build
                # a concrete edit scaffold (not a generic template).
                _blocked_file = ""
                _mf = _READ_FILE_RE.search(raw)
                if _mf:
                    _blocked_file = _mf.group(1)
                # Also try evidence cmd if blocked cmd has no .py path
                if not _blocked_file and self._evidence_cmd:
                    _mf2 = _READ_FILE_RE.search(self._evidence_cmd)
                    if _mf2:
                        _blocked_file = _mf2.group(1)
                evidence = ""
                if self._evidence_out:
                    evidence = (
                        "\n--- YOUR EARLIER CODE EVIDENCE (you collected this) ---\n"
                        f"$ {self._evidence_cmd}\n"
                        f"{self._evidence_out}\n"
                        "-------------------------------------------------------\n"
                        "Turn one of those lines into an edit NOW — craft the NEW\n"
                        "fixed text yourself and write it:\n"
                    )
                # Build concrete scaffold with the actual file path
                if _blocked_file:
                    _file_hint = _blocked_file
                    _sed_template = (
                        f"  1. sed -i 's/OLD_LINE/NEW_FIXED_LINE/' {_file_hint}\n"
                        f"  2. DockerEdit.replace(file='{_file_hint}', old='verbatim', new='fixed')\n"
                    )
                else:
                    _sed_template = (
                        "  1. sed -i 's/OLD_TEXT_EXACTLY/NEW_FIXED_TEXT/' <file>\n"
                        "  2. DockerEdit.replace(file='<file>', old='verbatim', new='fixed')\n"
                    )
                return (
                    "\n=====================================================\n"
                    f"EDIT DEADLINE EXCEEDED — round {self._act_count}, 0 edits.\n"
                    "Non-essential commands are BLOCKED. Use sed -n/grep to\n"
                    "read code, then sed -i/DockerEdit to edit, then submit:\n"
                    f"{evidence}"
                    f"{_sed_template}"
                    "  3. submit  (only after edits landed)\n"
                    "Copy OLD_TEXT verbatim from the evidence above (exact\n"
                    "indentation). EDIT A FILE NOW.\n"
                    "=====================================================\n"
                )

        # ---- Phased whitelist (legacy explore_budget gate) -------------------
        in_write_phase = (
            self._act_count > self.explore_budget and not self._unlocked
        )
        if in_write_phase and not _WRITE_ALLOWED.match(raw):
            logger.info(f"gate blocked read-only cmd in write phase: {raw[:60]}")
            return (
                "[Exploration budget exhausted] Read-only commands are now "
                "BLOCKED. Edit the code NOW with a plain bash write such as "
                "`sed -i 's/old/new/' <file>` or "
                "DockerEdit.replace(file, old, new). When all fixes are in "
                "place, run `git diff` to verify, then `submit`."
            )
        is_write_attempt = _looks_like_edit(raw) or "DockerEdit" in raw
        self._last_write_counted = False
        # D2 fix: snapshot the working tree BEFORE the first write attempt of
        # this run. Later baselines reuse the post-write snapshot (non-write
        # commands cannot change tracked files, so the snapshot stays valid).
        baseline = self._diff_after_last_write
        if is_write_attempt and baseline is None:
            _pre = await self.terminal.run(cmd=_STATUS_CMD)
            baseline = _clean_status(_pre if isinstance(_pre, str) else str(_pre or ""))
        out = await self.terminal.run(cmd=cmd, **kwargs)
        # smoke48 anchor-enhancement: remember the latest successful
        # grep / sed -n output so deadline BLOCKs can echo Alex's own
        # code evidence back at him (anchors-only, no gold semantics).
        if out and len(out) > 20 and _EVIDENCE_CMD.search(raw):
            _ev_lines = [ln for ln in out.splitlines() if ln.strip()][:12]
            _ev_text = "\n".join(_ev_lines)[:1200]
            if _ev_text:
                self._evidence_cmd = raw[:200]
                self._evidence_out = _ev_text
        # ---- Write-post verification (D2 fix) ------------------------------
        # A bash write that changes NOTHING must not count as an edit, must
        # not unlock the toolset, and must trigger a recovery hint instead.
        if is_write_attempt:
            verified = "REPLACE_OK" in out or "MULTI_REPLACE_OK" in out
            if not verified:
                _post_raw = await self.terminal.run(cmd=_STATUS_CMD)
                _post = _clean_status(_post_raw if isinstance(_post_raw, str) else str(_post_raw or ""))
                self._diff_after_last_write = _post
                # If git itself is unavailable (non-docker/unit contexts) we
                # cannot verify — fall back to the pre-audit behaviour
                # (count the edit) instead of blocking every write.
                if _GIT_FAIL.search(_post):
                    verified = True
                else:
                    verified = _post != baseline
            if verified:
                self._last_write_counted = True
                self._unlocked = True
                logger.info(
                    f"verified edit (working tree changed) — tools unlocked, "
                    f"edits={self._edits_count + 1}"
                )
                # ① iteration nudge (verify-rerun finding): 23/42 still-failed
                # cases made EXACTLY ONE verified edit and never ran the
                # affected tests — 5 were one test away from RESOLVED. On the
                # FIRST verified edit tell Alex to run the relevant test file
                # and iterate; each further edit resets the force-submit idle
                # clock, so iterating is always safe.
                if self._edits_count == 0:
                    out = out + _ITERATION_NUDGE
            else:
                logger.warning(
                    f"write attempt produced NO working-tree change: cmd[:80]={raw[:80]}"
                )
                # Detect sed cross-line pattern (\n in pattern) — sed processes
                # line-by-line and cannot match across lines. Guide to DockerEdit.
                _has_cross_line = bool(re.search(r"\\n", raw)) and bool(re.match(r".*sed\s+-i", raw))
                _file_hint = _READ_FILE_RE.search(raw)
                _file_hint = _file_hint.group(1) if _file_hint else "<file>"
                if _has_cross_line:
                    out = out + (
                        "\n[WRITE FAILED — 0 lines changed] Your sed -i used \\n "
                        "(cross-line pattern) but sed processes line-by-line and "
                        "CANNOT match across lines. Use DockerEdit.replace instead:\n"
                        f"  DockerEdit.replace(file='{_file_hint}', old='verbatim multi-line text', new='fixed text')\n"
                        "Copy OLD text verbatim from your earlier grep/sed -n output "
                        "(exact indentation, include all relevant lines).\n"
                    )
                else:
                    out = out + (
                        "\n[WRITE FAILED — 0 lines changed] Your edit command ran but "
                        "the working tree did NOT change: OLD_TEXT probably didn't "
                        "match verbatim (exact indentation!) or the file path was "
                        f"wrong. This attempt did NOT count as an edit "
                        f"(edits={self._edits_count}). Retry with:\n"
                        f"  sed -i 's/OLD_TEXT_EXACTLY/NEW_FIXED_TEXT/' {_file_hint}\n"
                        "  (OLD_TEXT verbatim from your earlier grep/sed -n output)\n"
                        f"  or DockerEdit.replace(file='{_file_hint}', old='verbatim', new='fixed')"
                    )
        return out

    def _git_create_pull_stub(self, *args, **kwargs):
        return "In eval mode, use the `submit` command instead of git_create_pull."

    async def _format_instruction(self):
        """Inject an editing nudge in the instruction when the agent idles on
        editing: either never edited for EDIT_NUDGE_THRESHOLD rounds, or has
        gone EDIT_NUDGE_THRESHOLD rounds since its LAST edit (recurring).
        Weak models tend to loop on search/open/grep indefinitely — and, after
        one successful edit, to drift into verification/archaeology loops. The
        nudge is appended to cmd_prompt_current_state (not injected into
        memory) to avoid polluting the conversation history.
        """
        await super()._format_instruction()
        idle_rounds = (
            self._act_count
            if not self._edits_count
            else self._act_count - self._round_of_last_edit
        )
        if idle_rounds < EDIT_NUDGE_THRESHOLD:
            return
        self.cmd_prompt_current_state += (
            f"\n\n[!] ACTION REQUIRED: You have spent {idle_rounds} commands "
            "since your last edit without editing anything new. If more files "
            "still need fixes, your next command MUST be an edit — plain bash "
            "like `sed -i 's/old/new/' <file>` or python3, or "
            "DockerEdit.replace(file, old, new). "
            "If ALL required edits are done, run `git diff` then `submit` NOW."
        )

    async def _think(self) -> bool:
        """Silence the role once force-submitted — UNLESS the Reviewer sent
        feedback (CodeReviewFeedback), which re-arms the Engineer for another
        editing pass. Each env-bus news re-enters _react which resets rc.todo
        (_set_state(0)), so todo=None alone cannot stop the loop — smoke12
        burned 3 forced submits + LLM think calls on residual news. With
        _force_done, _react breaks before any LLM call. The re-arm scan
        itself lives in _maybe_rearm_on_feedback (called from _react, where
        it can actually fire — see that method's docstring)."""
        if self._force_done:
            self._maybe_rearm_on_feedback()
            if self._force_done:
                return False
        if self.use_native_toolcall:
            return await self._think_native_toolcall()
        return await super()._think()

    async def _think_native_toolcall(self) -> bool:
        return await swe_protocol.think(self)

    async def _force_submit(self, reason: str) -> Message:
        if self.use_native_toolcall:
            await swe_protocol.submit(self, reason)
            return AIMessage(content="", cause_by=RunCommand)
        self._force_done = True
        self.rc.todo = None
        await self._parse_commands_for_eval()
        return AIMessage(content="Forced submit: " + reason, cause_by=RunCommand)

    async def _act(self) -> Message:
        """Wrap upstream _act with JSONDecodeError/KeyError/TypeError tolerance,
        edit detection, spiral guards, and MAS handoff.
        """
        if self.use_native_toolcall:
            return await swe_protocol.act(self)

        # Empty-response breaker: _think tripped after a long consecutive
        # empty streak. Route through _force_submit so the returned Message
        # flows exactly like the idle trigger (MAS handoff included) instead
        # of burning the remaining wall clock on 4-empty-call rounds.
        if getattr(self, "_empty_breaker", False):
            self._empty_breaker = False
            return await self._force_submit(
                reason=f"empty-response breaker ({self._consecutive_empty} consecutive empty rounds)"
            )

        # Tier 2 skip: _think flagged an empty-response round. Don't count
        # it, don't run any tool, don't pollute memory — just signal back
        # and let react() retry think on the next loop. The next prompt will
        # include a targeted "stop returning empty" warning via consecutive_empty.
        if getattr(self, "_skip_round", False):
            self._skip_round = False
            return AIMessage(
                content="(LLM returned empty; retrying without counting this round)",
                sent_from=self.name,
                cause_by=RunCommand,
            )

        self._act_count += 1
        # EvoMAS trigger (B): editing stalled — never edited, or no new edit
        # for FORCE_SUBMIT_IDLE rounds despite recurring nudges. Stop the spin.
        # smoke46 fix: with 0 edits the idle clock used to start at round 0,
        # so force-submit fired at R15 — only ~4 rounds AFTER the edit deadline
        # (R10) had switched the gates into write-only mode. The deadline gate
        # never got real teeth. Now, when no edit has happened yet, the idle
        # clock starts at EDIT_DEADLINE_ROUND: Alex gets the full
        # FORCE_SUBMIT_IDLE (15) rounds of write-only pressure after the
        # deadline (fires at R25 with n_round 30), matching the pressure under
        # which smoke39 produced its patch. If an edit HAS happened, idle is
        # measured from the last real edit as before.
        idle_base = (
            self._round_of_last_edit if self._edits_count else EDIT_DEADLINE_ROUND
        )
        # _idle_floor: pushed forward on Reviewer-feedback re-arm so the
        # revise window is REARM_GRACE_ROUNDS, not zero (see
        # _maybe_rearm_on_feedback).
        idle_rounds = self._act_count - max(idle_base, self._idle_floor)
        if self.run_eval and idle_rounds >= FORCE_SUBMIT_IDLE:
            return await self._force_submit(
                f"{idle_rounds} rounds without a new edit"
            )
        # EvoMAS trigger (A): JSON collapse — the model failed to emit a valid
        # command array MAX_JSON_FAIL_STREAK times in a row. Only after a warm
        # up of 10 rounds: early JSON wobbles are the norm for weak models and
        # repairs DO recover them (smoke7: 12 repairs, still finished edits);
        # triggering earlier amputates an agent that never got to edit.
        if (
            self.run_eval
            and self._act_count >= 10
            and self._json_fail_streak >= MAX_JSON_FAIL_STREAK
        ):
            return await self._force_submit(
                f"{self._json_fail_streak} consecutive JSON failures"
            )
        if self.use_native_toolcall and hasattr(self, "_native_rsp"):
            message = await self._act_native_toolcall()
        else:
            try:
                message = await super()._act()
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                self._json_fail_streak += 1
                err = (
                    f"Your last output was not valid JSON or was missing a required key ({type(e).__name__}: {e}). "
                    "Output ONLY a ```json block containing a valid command array. "
                    "Each command object MUST have 'command_name' and 'args' keys."
                )
                self.rc.memory.add(UserMessage(content=err, cause_by=RunCommand))
                return AIMessage(content=err, sent_from=self.name, cause_by=RunCommand)
        self._json_fail_streak = 0
        if self.command_rsp and '"cmd": "edit' in self.command_rsp:
            self._edits_count += 1
            self._round_of_last_edit = self._act_count
        if self.run_eval:
            await self._parse_commands_for_eval()
        return message

    async def _act_native_toolcall(self) -> Message:
        return await swe_protocol.act(self)

    async def _parse_commands_for_eval(self):
        """Collect git diff when todo=None. In MAS mode, also publish
        CodeReviewRequest to hand off to the Reviewer."""
        if self.use_native_toolcall:
            return  # The native submit event already collected and handed off the patch.
        if not self.rc.todo:
            from metagpt.tools.swe_agent_commands.swe_agent_utils import extract_patch

            try:
                # HEAD (not --cached): agent edits are never staged, so
                # --cached returns an empty diff and every patch would depend
                # on the runner's finally-block salvage. Exclude .gitignore:
                # the runner seeds '.backup.*' into it at startup, which would
                # otherwise pollute every patch with a noise hunk.
                diff_output = await self.terminal.run(
                    "git diff HEAD -- . ':(exclude).gitignore'"
                )
                clear_diff = extract_patch(diff_output)
                logger.info(f"Diff output: \n{clear_diff}")
                if clear_diff:
                    self.output_diff = clear_diff
            except Exception as e:
                logger.error(f"Error during submission: {e}")

            # Always hand off to Reviewer in MAS mode, even with an empty
            # diff. The Reviewer's verdict (iterate vs approve) is the
            # only authoritative signal for Phase 3. Previously smoke28:
            # Alex force-submitted with no diff, no Reviewer handoff,
            # Mike saw "Forced submit" and re-entered exploration.
            if self.mas_mode and self.rc.env:
                msg = Message(
                    content=(
                        "I've finished editing the code in the repository. "
                        "Please review the changes by running `git diff HEAD` "
                        "and check whether the fix addresses the issue."
                    ),
                    sent_from=self.name,
                    send_to={"Reviewer"},
                    cause_by=CodeReviewRequest,
                )
                self.rc.env.publish_message(msg, publicer=self.profile)
                logger.info("MAS mode: published CodeReviewRequest to Reviewer.")
