"""Simple CLI entry point for rsba1-core."""
import sys
import argparse

SUPPORTED_COMMANDS = {"read-freq", "read-smeter", "read-mode", "set-freq", "set-mode", "ptt", "get-status"}


def main():
    parser = argparse.ArgumentParser(description="rsba1-core CLI — IC-705 control")
    parser.add_argument("command", choices=list(SUPPORTED_COMMANDS), help="Command to run")
    parser.add_argument("args", nargs="*", help="Arguments for the command")
    parsed = parser.parse_args()

    print(f"[rsba1-core] command={parsed.command} args={parsed.args}", file=sys.stderr)
    print("CLI placeholder — use MCP server or import rsba1 modules directly", file=sys.stderr)
    print("Example: python -m rsba1.mcp  (starts MCP server)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
