"""
Shared local Git-execution hardening for kernel/tools/handlers/ (extracted
from repo_health.py in Milestone 34; reused by repository_backup.py in
Milestone 35).

This module covers only the protections every *local* git subprocess this
codebase runs needs - a fixed safety-flag prefix and a sanitized process
environment. It intentionally does not include repo_health.py's
remote-only machinery (the GitHub-origin allowlist, the neutral-cwd
network isolation, proxy stripping, credential/askpass/transport
overrides) - repository_backup.py never makes a network call, so it has
no use for any of that, and duplicating it here would just be dead code.
See repo_health.py's own module docstring for the full remote-call
rationale.

GIT_SAFE_PREFIX supplies --no-optional-locks, --no-pager,
--no-replace-objects, and -c core.fsmonitor=false on the command line -
global git options, not config keys - so nothing in a target repository's
own .git/config can shadow them: a command-line -c always wins over repo
config in git's own resolution order. --no-replace-objects means a
repository-configured replace ref can never substitute a different object
for the one actually read or bundled.

sanitized_git_env() starts from a full copy of this process's real
environment (PATH and everything else ordinary stays available, never
replaced with a fabricated minimal mapping), strips every variable in the
blocklist below (matched case-insensitively, since environment variable
names are case-insensitive on Windows), then layers on
GIT_OPTIONAL_LOCKS=0, GIT_CONFIG_NOSYSTEM=1, and
GIT_CONFIG_GLOBAL=os.devnull - no system- or machine-global git config is
ever consulted by any call built on this function, though a local call
still reads the target repository's own *local* config where a caller
needs it (e.g. repo_health.py reading remote.origin.url), since
GIT_CEILING_DIRECTORIES is stripped, not set, here.
"""

import os

# Fixed prefix for every local git invocation built on this module.
# Deliberately supplied as argv (not left to config files) - see module
# docstring.
GIT_SAFE_PREFIX = [
    "git",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "-c", "core.fsmonitor=false",
]

# Environment variables that could inject additional git configuration,
# redirect which credential/SSH/askpass program git runs, redirect
# repository discovery/refs/index/object storage, disable transport
# security, or leak transport detail via tracing/redirection - stripped
# from every git subprocess's environment regardless of how they got set.
GIT_ENV_BLOCKLIST_EXACT = {
    # config/credential injection
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "SSH_ASKPASS",
    # repository/ref/object/index redirection
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CEILING_DIRECTORIES",
    "GIT_REPLACE_REF_BASE",
    # transport security / tracing / stdio redirection
    "GIT_SSL_NO_VERIFY",
    "GIT_CURL_VERBOSE",
    "GIT_REDIRECT_STDIN",
    "GIT_REDIRECT_STDOUT",
    "GIT_REDIRECT_STDERR",
}
GIT_ENV_BLOCKLIST_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE")


def sanitized_git_env() -> dict:
    """See module docstring. Callers that need additional controlled
    values on top of this result (e.g. repo_health.py's remote-only
    GIT_CEILING_DIRECTORIES/GIT_TERMINAL_PROMPT/GCM_INTERACTIVE overlay)
    add them to the returned mapping themselves."""

    env = os.environ.copy()
    for key in list(env):
        upper_key = key.upper()
        if upper_key in GIT_ENV_BLOCKLIST_EXACT or upper_key.startswith(GIT_ENV_BLOCKLIST_PREFIXES):
            del env[key]
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env
