"""
create_directory action handler (Milestone 43 P2): creates exactly one
pre-authorized, pre-named directory (ToolsConfig.approved_directory_creations)
directly inside an already-approved parent directory. resource_key selects
the ENTIRE operation - a (parent, child name) pair fixed by configuration -
nothing here accepts a caller/model-supplied parent, name, nesting depth,
or overwrite flag. No path is ever derived from request text, description,
expected_result, or model output; the only input this handler ever reads
is request.resource_key.

Create-only: if anything already occupies the target name - a directory,
a file, a symlink, or a dangling symlink/reparse point - this handler
fails closed rather than merging, succeeding idempotently, or overwriting.
There is no recursive parent creation (no mkdir(parents=True)) and no
"create if missing, else succeed" semantics.

Mechanics: kernel/tools/file_safety.py's resolve_approved_directory()
resolves and freshly validates the parent (rejecting a symlink/reparse
point or a target that is not currently a directory) exactly as it does
for copy_file.py's destination directory. This handler then combines that
resolved parent with the configured, pre-validated directory_name and
attempts exactly one os.mkdir() - the same atomic, race-free "fail if it
already exists" primitive every no-clobber operation in kernel/tools/
ultimately depends on. A pre-check via
file_safety.path_exists_including_dangling_links() gives a fast, clean
rejection for the common case, but os.mkdir()'s own FileExistsError -
not the pre-check - is the actual authority: see file_safety.py's module
docstring for why a pre-check alone can never be race-free.
"""

from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH, is_valid_child_name
from kernel.tools.file_safety import (
    FileResourceError,
    path_exists_including_dangling_links,
    resolve_approved_directory,
)
from kernel.tools.types import ActionRequest, ActionResult

_UNKNOWN_OPERATION = ActionResult(
    False, "That directory creation is not registered.", "rejected"
)
_PARENT_UNAVAILABLE = ActionResult(False, "The parent directory is not available.", "failed")
_TARGET_EXISTS = ActionResult(False, "That directory already exists.", "failed")
_CREATION_FAILED = ActionResult(False, "The directory could not be created.", "failed")


def run(request: ActionRequest, tools_config) -> ActionResult:
    key = request.resource_key
    if key is None:
        return _UNKNOWN_OPERATION

    spec = tools_config.approved_directory_creations.get(key)
    if spec is None:
        return _UNKNOWN_OPERATION

    if not is_valid_child_name(spec.directory_name):
        # Defense in depth against a ToolsConfig ever built without going
        # through load_tools_config(), which already enforces this at
        # config-load time - see kernel/tools/config.py's own
        # _parse_create_directory(), and repository_backup.py's identical
        # is_valid_backup_key() re-check precedent.
        return _CREATION_FAILED

    if len(key) > MAX_SYMBOLIC_NAME_LENGTH or len(spec.parent_directory_key) > MAX_SYMBOLIC_NAME_LENGTH:
        # Defense in depth, same reasoning as above: load_tools_config()
        # already bounds every symbolic identifier's length (see
        # kernel/tools/config.py's MAX_SYMBOLIC_NAME_LENGTH). Without this,
        # a hand-built ToolsConfig with an absurdly long key/reference
        # could still execute successfully - the real mkdir() call only
        # ever touches the short, validated directory_name - while
        # producing an ActionResult.message too large to fit
        # MAX_STEP_RESULT_JSON_CHARS once wrapped in a StepObservation,
        # discovered only AFTER the real directory was already created
        # (kernel/task_execution/service.py's ACTION-step finalize path
        # does not catch that the way the RESPOND path does).
        return _CREATION_FAILED

    try:
        resolved_parent = resolve_approved_directory(spec.parent_directory_key, tools_config)
    except FileResourceError:
        return _PARENT_UNAVAILABLE

    target_path = resolved_parent.path / spec.directory_name

    if path_exists_including_dangling_links(target_path):
        return _TARGET_EXISTS

    try:
        target_path.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return _TARGET_EXISTS
    except OSError:
        return _CREATION_FAILED

    message = (
        "Directory created.\n"
        f"Operation: '{key}'\n"
        f"Parent: '{spec.parent_directory_key}'\n"
        f"Name: '{spec.directory_name}'"
    )
    return ActionResult(True, message, "executed")
