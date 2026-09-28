"""Restrict generation to base snapshots, preserving Git IDs and submodules."""
import re
import shlex


def isolate_history_command(base_commit, image_snapshot_commit=None):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', base_commit):
        raise ValueError('A full base commit SHA is required')
    if image_snapshot_commit is not None and not re.fullmatch(r'[0-9a-fA-F]{40}', image_snapshot_commit):
        raise ValueError('A full audited image snapshot SHA is required')
    # Verified images may wrap the base tree plus build compatibility changes
    # in a synthetic "SWE-bench" commit. Only accept the exact preflight-audited
    # snapshot, preserve its working tree, and remove that commit below.
    align = ''
    if image_snapshot_commit:
        align = '''if test "$(git rev-parse HEAD)" != "$base"; then
    test "$(git rev-parse HEAD)" = '''+shlex.quote(image_snapshot_commit)+'''
    test "$(git log -1 --format=%s)" = 'SWE-bench'
    git reset --soft "$base"
fi
# Official image snapshots can chmod the whole checkout during packaging.
# Match the harness's core.fileMode=false comparison against the task base.
git config core.filemode false
'''
    return '''set -eu
base='''+shlex.quote(base_commit)+'''
root=$(git rev-parse --show-toplevel)
'''+align+'''test "$(git rev-parse HEAD)" = "$base"
test -d "$root/.git"
test ! -L "$root/.git"
test "$(git rev-parse --absolute-git-dir)" = "$root/.git"
isolate_one() {
    local repo="$1" commit="$2" scratch option value
    scratch=$(mktemp -d /tmp/swe-base-git.XXXXXXXX)
    git init -q "$scratch"
    git -C "$scratch" -c protocol.file.allow=always fetch -q --no-tags --depth=1 "file://$repo" "$commit"
    git -C "$scratch" update-ref HEAD "$commit"
    git -C "$scratch" read-tree HEAD
    for option in core.filemode core.symlinks core.ignorecase core.autocrlf core.eol; do
        if value=$(git -C "$repo" config --get "$option"); then
            git -C "$scratch" config "$option" "$value"
        fi
    done
    test "$(git -C "$scratch" rev-list --all --count)" = 1
    test ! -s "$scratch/.git/objects/info/alternates"
    rm -rf -- "$repo/.git"
    mv "$scratch/.git" "$repo/.git"
    rmdir "$scratch"
    git -C "$repo" config user.name 'SWE Agent'
    git -C "$repo" config user.email 'agent@example.invalid'
    test "$(git -C "$repo" rev-parse HEAD)" = "$commit"
}
# Convert initialized submodule gitfiles to standalone shallow repositories
# before removing the superproject .git/modules database. Deepest first also
# preserves nested submodules without retaining their future objects.
module_list=$(mktemp /tmp/swe-submodules.XXXXXXXX)
git submodule foreach --quiet --recursive 'printf "%s\\0" "$PWD"' > "$module_list"
mapfile -d '' -t submodules < "$module_list"
rm -f "$module_list"
for ((idx=${#submodules[@]}-1; idx>=0; idx--)); do
    module="${submodules[$idx]}"
    isolate_one "$module" "$(git -C "$module" rev-parse HEAD)"
done
isolate_one "$root" "$base"
printf 'HISTORY_ISOLATED base=%s commits=%s submodules=%s\\n' "$base" "$(git rev-list --all --count)" "${#submodules[@]}"
'''
