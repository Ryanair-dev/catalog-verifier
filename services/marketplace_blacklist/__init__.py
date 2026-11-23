"""Marketplace Blacklist: SKUs / Listing IDs blocked per store, in Azure SQL analytics.blacklist_listings.

Ported from the standalone app in blacklist/app/ (models, matching, repository, sql_repository are
copied unchanged apart from this package). Routes live in routers/marketplace_blacklist.py.
NOT the Pair-Manager blacklisted_pairs table. See docs/features/blacklist.md.
"""
