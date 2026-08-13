"""Test-only fixture: exits with a non-zero status immediately - simulates
a resolver failure. Never used by production code."""

import sys

if __name__ == "__main__":
    sys.exit(1)
