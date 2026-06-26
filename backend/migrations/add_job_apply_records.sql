-- Migration: add job apply records table for auto-apply logs
-- Run this against the existing PostgreSQL database.

CREATE TABLE IF NOT EXISTS job_apply_records (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(255),
    site VARCHAR(64),
    job_url TEXT,
    resume_language VARCHAR(8),
    resume_file VARCHAR(255),
    details JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_job_apply_records_job_id ON job_apply_records(job_id);
CREATE INDEX IF NOT EXISTS idx_job_apply_records_created_at ON job_apply_records(created_at);
CREATE INDEX IF NOT EXISTS idx_job_apply_records_status ON job_apply_records(status);
