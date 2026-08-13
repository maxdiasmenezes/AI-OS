"""Test-only fixture: a DNS-worker-shaped script that always sleeps far
longer than any test timeout. Used ONLY to prove
fresh_dns_safety_check()'s subprocess-based timeout genuinely bounds
wall-clock time and genuinely kills the child on timeout - never used by
production code, never referenced outside tests/kernel/tools/."""

import sys
import time

if __name__ == "__main__":
    time.sleep(30)
    sys.exit(0)
