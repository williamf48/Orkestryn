-- Create once in the relay's dedicated Cloudflare D1 database.
-- Never reset this ledger to retry an uncertain or failed provider request.
CREATE TABLE IF NOT EXISTS relay_requests (
  username TEXT NOT NULL,
  workflow TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('factory','governance','recovery')),
  request_id TEXT NOT NULL,
  binding TEXT NOT NULL,
  expires_ms INTEGER NOT NULL,
  created_ms INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('STARTED','RECEIVED','UNCONFIRMED')),
  result_json TEXT,
  PRIMARY KEY (username, workflow, role),
  UNIQUE (username, request_id)
);
