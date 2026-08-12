"""
file_metadata action handler (Milestone 43 P1): bounded, safe metadata for
exactly one individually registered file (ToolsConfig.approved_files) -
never a directory allowlist. See kernel/tools/file_safety.py's own module
docstring for the shared resolution/validation logic this and
read_text_file.py both use, and its residual-TOCTOU-limitation discussion.

Never reads file content - only the single lstat() resolve_approved_file()
already performs. The configured absolute path is never included in
ActionResult.message - only the caller's own symbolic key plus metadata
already considered safe to relay (size, modification time, extension).
"""

from kernel.tools.config import MAX_SYMBOLIC_NAME_LENGTH
from kernel.tools.file_safety import FileResourceError, resolve_approved_file
from kernel.tools.types import ActionRequest, ActionResult

# Same message/outcome resolve_approved_file() already returns for a key
# that is simply absent from approved_files - deliberately indistinguishable
# from an ordinary "not registered" rejection, so this never reveals that
# length was the specific reason. Defense in depth against a ToolsConfig
# ever built without going through load_tools_config() (which already
# bounds every approved_files key via _parse_approved_files()'s own
# max_length=MAX_SYMBOLIC_NAME_LENGTH - see that constant's own docstring
# for why: this handler echoes resolved.key verbatim into a successful
# ActionResult.message, which must always fit MAX_STEP_RESULT_JSON_CHARS).
_UNREGISTERED = ActionResult(False, "That file is not registered.", "rejected")


def run(request: ActionRequest, tools_config) -> ActionResult:
    if request.resource_key is not None and len(request.resource_key) > MAX_SYMBOLIC_NAME_LENGTH:
        return _UNREGISTERED

    try:
        resolved = resolve_approved_file(request.resource_key, tools_config)
    except FileResourceError as exc:
        return ActionResult(False, exc.message, exc.outcome)

    extension = resolved.path.suffix or "(none)"

    lines = [
        f"File: '{resolved.key}'",
        "Type: regular file",
        f"Size: {resolved.size_bytes} bytes",
        f"Modified: {resolved.modified_at}",
        f"Extension: {extension}",
    ]
    return ActionResult(True, "\n".join(lines), "executed")
