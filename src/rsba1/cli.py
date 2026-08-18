"""rsba1 CLI entry point — delegates to mcp.__main__:main for the actual implementation."""
import sys
from rsba1.mcp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
