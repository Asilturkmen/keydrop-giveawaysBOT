-- Keydrop Bot anonymous usage stats (Cloudflare D1).
-- New database: npx wrangler d1 execute keydrop-stats --remote --file schema.sql
-- A database made before v1.4.0 also needs: ... --file migrate-v1.4.sql

-- One row per installation (bot folder). `id` is a random number the app made up on its
-- first start; `device` is another random number, shared by every folder on the same
-- computer (not tied to any person or account). No IP address is stored anywhere.
CREATE TABLE IF NOT EXISTS installs (
  id         TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL,   -- unix seconds
  last_seen  INTEGER NOT NULL,
  version    TEXT,
  lang       TEXT,
  country    TEXT,               -- 2-letter code from Cloudflare
  device     TEXT,               -- NULL for v1.3.0 and older
  joins      INTEGER NOT NULL DEFAULT 0   -- giveaways this copy joined (v1.4.0+)
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
