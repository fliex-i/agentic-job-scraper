-- Migration: add source published/updated time fields to jobs
-- Run this against the existing PostgreSQL database.

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMP NULL;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMP NULL;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS source_published_text VARCHAR(255) NULL;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS source_updated_text VARCHAR(255) NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_source_published_at ON jobs(source_published_at);
CREATE INDEX IF NOT EXISTS idx_jobs_source_updated_at ON jobs(source_updated_at);
