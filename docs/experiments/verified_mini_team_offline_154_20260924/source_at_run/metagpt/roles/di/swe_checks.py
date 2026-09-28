"""Dependency-free execution evidence helpers; classifications are not verdicts."""
import re
import shlex

# Capture the real command status BEFORE printing bounded output. The command
# supplied to this wrapper should be the actual test, without head/tail pipes.
def capture_check(command):
    return (
        'swe_check_log=$(mktemp /tmp/swe-check.XXXXXXXX)\n'
        'bash -o pipefail -c ' + shlex.quote(command) + ' >"$swe_check_log" 2>&1\n'
        'swe_check_rc=$?\n'
        'printf "[check log: %s]\\n" "$swe_check_log"\n'
        'if [ "$(wc -c < "$swe_check_log")" -le 12000 ]; then cat "$swe_check_log"; '
        'else head -c 5000 "$swe_check_log"; printf "\\n[output truncated]\\n"; tail -c 7000 "$swe_check_log"; fi\n'
        'printf "\\n[check exit code: %s]\\n" "$swe_check_rc"\n'
        'exit "$swe_check_rc"'
    )


def is_test_command(command):
    """Recognize executable test runners, never words printed by cat/sed/grep.

    This is deliberately conservative, not a shell interpreter. Explicit
    swe-check remains the supported way to record arbitrary reproductions.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    segments = [[]]
    for token in tokens:
        if token and set(token) <= set('; &|\n'):
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        while segment and (segment[0] == 'env' or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', segment[0])):
            segment = segment[1:]
        if not segment:
            continue
        program = segment[0].rsplit('/', 1)[-1]
        args = segment[1:]
        if program in ('swe-check', 'pytest', 'py.test', 'ansible-test', 'jest'):
            return True
        if re.fullmatch(r'python(?:[0-9.]+)?', program) and any(
                args[i] == '-m' and args[i+1] in ('pytest', 'unittest') for i in range(len(args)-1)):
            return True
        if program == 'go' and args[:1] == ['test']:
            return True
        if program in ('npm', 'yarn', 'pnpm') and (args[:1] in (['test'], ['jest']) or args[:2] in (['run', 'test'], ['run', 'jest'])):
            return True
    return False


def check_evidence(command, result):
    """Recognize explicit test summaries, including errors masked by a pipeline."""
    output = result.get('output', '')
    rc = result.get('returncode', result.get('exit_code'))
    test_like = is_test_command(command)
    if not test_like:
        # A real failed Python reproduction is evidence too. Reading a source
        # file that mentions AssertionError, or a timed-out search, is not.
        traceback = re.search(r'^Traceback \(most recent call last\):', output, re.M)
        python_command = re.search(r'(?:^|&&|;|\n)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*python[0-9.]*\s', command)
        exception_line = re.search(r'^(?:AssertionError|TypeError|ValueError|AttributeError|ImportError|ModuleNotFoundError|SyntaxError)(?::|$)', output, re.M)
        if rc not in (None, 0, 124) and (traceback or (python_command and exception_line)):
            return dict(status='failure_observed', returncode=rc, summary=clip(output, 1600),
                        note='Runtime traceback from a reproduction, not a passing test.')
        return None
    status = 'unknown'
    if re.search(r'no tests (?:to run|ran)|collected 0 items|\[no test files\]', output, re.I):
        status = 'no_tests'
    elif re.search(r'\b[1-9]\d* failed\b|AssertionError|^FAIL\b|^FAILED\b|unrecognized arguments:', output, re.M):
        status = 'failure_observed'
    elif rc == 124:
        status = 'timeout'
    elif test_like and rc == 0 and re.search(r'\b[1-9]\d* passed\b|^ok\s|^PASS$|^OK$', output, re.M):
        status = 'tests_passed'
    elif test_like and rc not in (None, 0):
        status = 'command_failed'
    if not test_like and status == 'unknown':
        return None
    lines = [line for line in output.splitlines() if re.search(
        r'failed|passed|error|FAIL|PASS|no tests|no test files|^ok\s|^OK$|check exit', line, re.I)]
    return dict(status=status, returncode=rc, summary='\n'.join(lines[-12:])[-1600:],
                note='Output classification, not a correctness verdict. Relate failures to requirements and the recorded tree version.')


def clip(text, limit=1800):
    return text if len(text) <= limit else text[:limit//3]+'\n[truncated]\n'+text[-limit*2//3:]

CHECK_SCRIPT = '''#!/bin/bash
log=$(mktemp /tmp/swe-check.XXXXXXXX)
"$@" >"$log" 2>&1
rc=$?
printf "[check log: %s]\\n" "$log"
if [ "$(wc -c < "$log")" -le 12000 ]; then
    cat "$log"
else
    head -c 5000 "$log"
    printf "\\n[truncated]\\n"
    tail -c 7000 "$log"
fi
printf "\\n[check exit code: %s]\\n" "$rc"
exit "$rc"
'''
