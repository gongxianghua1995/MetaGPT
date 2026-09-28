"""Shared, dependency-free shell contract for the mini worker and team tools."""
def docker_shell_argv(container, cwd, command, seconds):
    return ['docker', 'exec', '-w', cwd, '-e', 'BASH_ENV=/root/.bashrc',
            container, 'timeout', '-k', '2', str(max(1, seconds)), 'bash', '-o', 'pipefail', '-c', command]
