-- Adds what v1.4.0 sends to a database made with the older schema.sql (run it once,
-- before deploying the new Worker). Nothing is deleted or changed.
-- npx wrangler d1 execute keydrop-stats --remote --file migrate-v1.4.sql
ALTER TABLE installs ADD COLUMN device TEXT;
ALTER TABLE installs ADD COLUMN joins INTEGER NOT NULL DEFAULT 0;
