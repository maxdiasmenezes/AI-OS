"""Test-only fixture: prints one fixed RFC1918 private IP address and
exits 0 - simulates a DNS resolution that returns a disallowed address.
Never used by production code."""

if __name__ == "__main__":
    print("10.0.0.5")
