import logging
import sys

from mcp_server.app import mcp
from mcp_server.tools import asin_research  # noqa: F401

logging.basicConfig(level=logging.INFO, stream=sys.stderr)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
