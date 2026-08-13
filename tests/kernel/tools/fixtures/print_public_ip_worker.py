"""Test-only fixture: prints one fixed, real, public IP address and exits
0 - simulates a DNS resolution that returns only safe/public addresses.
Never used by production code."""

if __name__ == "__main__":
    print("93.184.216.34")
