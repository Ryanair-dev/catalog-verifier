-- Proposed additions for analytics.blacklist_listings, derived from RestockR5.xlsx › BlacklistData.
-- Lookup-table column names below (channel_name, reason_name, identifier_type_name) are ASSUMED:
-- adjust them to the real pl_* table definitions before running.

-- 1. New columns ------------------------------------------------------------
ALTER TABLE analytics.blacklist_listings ADD
    asin VARCHAR(10) NULL          --ASIN of the listing a SKU row blocks; drives the 'review' check
  , created_by NVARCHAR(100) NULL  --who added it (Excel AddedBy; later the Entra user)
GO

-- Rows imported from Excel without a DateAdded have no known date: store NULL and show
-- "Not available" instead of inventing one. The defaults still stamp every new row.
ALTER TABLE analytics.blacklist_listings ALTER COLUMN created_at DATETIMEOFFSET(7) NULL
ALTER TABLE analytics.blacklist_listings ALTER COLUMN updated_at DATETIMEOFFSET(7) NULL
GO

CREATE NONCLUSTERED INDEX [IX_blacklist_listings_asin]
  ON analytics.blacklist_listings (asin) WHERE asin IS NOT NULL
GO

-- 2. Reference data -----------------------------------------------------------
INSERT INTO analytics.pl_channels (channel_id, channel_name) VALUES
    (1, N'Preferred Pharmacy Plus')
  , (2, N'Preferred Pharmacy Plus CA')
  , (3, N'Harper Wrens')
  , (4, N'Turba')
  , (5, N'WFS')
GO

INSERT INTO analytics.pl_identifier_types (identifier_type_code, identifier_type_name) VALUES
    (1, N'Seller SKU')
  , (2, N'ASIN')
  , (3, N'Brand prefix')
GO

INSERT INTO analytics.pl_blacklist_reason_codes (blacklist_reason_code_id, reason_name) VALUES
    (1, N'Do not send')
  , (2, N'Duplicate listing')
  , (3, N'Recall / safety')
  , (4, N'Restricted / gated')
  , (5, N'Brand request / agreement')
  , (6, N'Transparency')
  , (7, N'IP / counterfeit complaint')
  , (8, N'Pricing error')
  , (9, N'Unprofitable')
  , (10, N'Sold on another channel')
  , (11, N'Fulfillment method')
  , (12, N'Listing issue (wrong item / qty)')
  , (13, N'Internal request')
  , (14, N'Other')
GO
