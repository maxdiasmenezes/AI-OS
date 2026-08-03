"""
atomic_finalize_no_replace(): moves a fully-written temporary file to its
final, permanent name within the same directory, with an explicit
guarantee that an existing file already at the final name is never
replaced, truncated, or deleted. The call either succeeds - and the final
name did not exist before it was called - or it raises
FinalizeCollisionError (or another OSError, for a primitive that
genuinely isn't available on this filesystem) and the final name, if
anything was already there, is left byte-for-byte untouched.

Deliberately never uses os.replace() - that function's entire contract is
"replace whatever's there", the opposite of what a completed backup
requires. It also deliberately avoids relying on any *general, informal*
claim that "rename doesn't overwrite" (true on some platforms and false
on others); instead it picks a documented, platform-specific no-replace
primitive:

- On Windows (os.name == "nt"): os.rename(). Per CPython's own
  documentation, "on Windows, if dst exists a FileExistsError will always
  be raised" - this is os.rename()'s documented behavior on this
  platform (backed by calling MoveFileExW without
  MOVEFILE_REPLACE_EXISTING), not an assumption about renames in general.
- Elsewhere: os.link() (an atomic, no-replace hard-link create - it also
  fails with FileExistsError if the destination exists) followed by
  os.unlink() of the source, once the destination link exists. This is
  the standard POSIX no-clobber-rename idiom, and requires source and
  destination to be the same filesystem (guaranteed here: both live in
  the same approved destination directory) and that directory to support
  hard links (not guaranteed on every filesystem - see below).

Callers must always pass a source and destination inside the *same*
directory - this module makes no attempt at cross-filesystem atomicity,
and its guarantees do not hold otherwise.

If the no-replace primitive itself isn't available (e.g. a filesystem
that doesn't support hard links on a non-Windows platform), an OSError
other than FinalizeCollisionError propagates - callers must treat that
identically to a collision: no completed backup, only the caller's own
temporary file to clean up.
"""

import os
from pathlib import Path


class FinalizeCollisionError(Exception):
    """Raised when the destination path already exists. Deliberately
    carries no path or other detail in its message - callers must never
    need to inspect this exception's text to decide what happened, only
    its type, and nothing about it is ever logged or relayed."""


def atomic_finalize_no_replace(source: Path, destination: Path) -> None:
    """Finalize `source` (an existing, fully-written file) to
    `destination` (a name that must not already exist), atomically and
    without ever replacing an existing file. Raises FinalizeCollisionError
    if `destination` already exists; raises OSError for any other failure
    (including the no-replace primitive being unavailable on this
    filesystem). Never removes `source` on failure - the caller owns
    cleanup of its own temporary file in every case."""

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise FinalizeCollisionError() from exc
        return

    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise FinalizeCollisionError() from exc
    os.unlink(source)
