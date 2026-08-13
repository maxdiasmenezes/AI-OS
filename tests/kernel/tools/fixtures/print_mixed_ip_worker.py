"""Test-only fixture: prints one public and one private IP address (a
mixed resolution result) and exits 0 - simulates a hostname that resolves
to multiple addresses, only some of which are disallowed. Never used by
production code."""

if __name__ == "__main__":
    print("93.184.216.34")
    print("10.0.0.5")
