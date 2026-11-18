import logging
import sys

from mcp_server.app import mcp

# Importing these modules registers their tools and prompts on `mcp`.
# They look unused, but removing them leaves the server empty.
from mcp_server import mcp_prompts  # noqa: F401
from mcp_server.tools import (  # noqa: F401
    asin_research,
    brand_analytics,
    checks,
    create_skus,
    matching,
    pair_management,
    po_analytics,
    seller_analytics,
    vendor_offers,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()