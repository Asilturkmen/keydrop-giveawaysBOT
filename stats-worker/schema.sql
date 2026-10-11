-- Keydrop Bot anonymous usage stats (Cloudflare D1).
-- Apply with: npx wrangler d1 execute keydrop-stats --remote --file schema.sql

-- One row per installation. `id` is a random number the app made up on its first
-- start (not tied to any person or account). No IP address is stored anywhere.
CREATE TABLE IF NOT EXISTS installs (
  id         TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL,   -- unix seconds
  last_seen  INTEGER NOT NULL,
  version    TEXT,
  lang       TEXT,
  country    TEXT                -- 2-letter code from Cloudflare
);
CREATE INDEX IF NOT EXISTS installs_last_seen ON installs (last_seen);

-- Which installations were active on which day (for the "daily users" trend).
-- Rows older than 60 days are deleted.
CREATE TABLE IF NOT EXISTS daily (
  day TEXT NOT NULL,             -- YYYY-MM-DD (UTC)
  id  TEXT NOT NULL,
  PRIMARY KEY (day, id)
) WITHOUT ROWID;

-- Counters and cached values: 'joins' (num), 'downloads' (num), 'stats' (text = JSON).
CREATE TABLE IF NOT EXISTS meta (
  key     TEXT PRIMARY KEY,
  num     INTEGER NOT NULL DEFAULT 0,
  text    TEXT,
  updated INTEGER NOT NULL DEFAULT 0
);
