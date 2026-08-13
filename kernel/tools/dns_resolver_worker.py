"""
Standalone, fixed, code-owned DNS resolution worker for
kernel/tools/browser_safety.py's fresh_dns_safety_check() defense-in-depth
check (Milestone 44 P1 correction). Invoked ONLY via
kernel/tools/process_control.py's run_capturing_stdout() as a real OS
subprocess - never a Python thread - so a hung resolver call can actually
be killed on timeout by that module's own _terminate_and_reap(), the same
primitive already used by open_application/run_registered_script/
repo_health/repository_backup (see kernel/tools/process_control.py's own
docstring). Always shell=False, list-form argv; the hostname is passed as
a single argv element - never interpolated into a shell string, never
executable code, and never model/request-text-supplied (the caller,
fresh_dns_safety_check(), only ever passes an already-parsed hostname
extracted from a config-authored URL).

Prints exactly one resolved IP address string per line to stdout (a small,
fixed, bounded output format - see run_capturing_stdout()'s own
max_output_bytes) and exits 0 on success. Prints nothing and exits 1 on
any resolution failure or invalid invocation - never the raw resolver
exception, so the parent process (and, through it, ActionResult/audit)
never sees anything beyond "some addresses" or "failure".

This module has no dependency on kernel.tools.browser_safety or any other
project package - it is a leaf script, importable and runnable in
isolation, with no side effects beyond one DNS lookup.
"""

import socket
import sys


def _resolve(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None)
    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            addresses.append(ip)
    return addresses


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 1
    try:
        addresses = _resolve(argv[1])
    except OSError:
        return 1
    if not addresses:
        return 1
    for address in addresses:
        print(address)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
