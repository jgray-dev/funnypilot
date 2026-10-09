-- One row per one-minute loggerd segment. The manifest is the exact JSON the
-- device sent; R2 keys are drives/<route>/<segment>/<artifact>/<part>.
CREATE TABLE IF NOT EXISTS segments (
 route TEXT NOT NULL, segment INTEGER NOT NULL, created_at REAL NOT NULL,
 commit_sha TEXT NOT NULL, bytes INTEGER NOT NULL, manifest TEXT NOT NULL,
 upload_status TEXT NOT NULL DEFAULT 'uploading',
 received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(route, segment)
);
CREATE INDEX IF NOT EXISTS segments_time ON segments(upload_status, created_at);
CREATE INDEX IF NOT EXISTS segments_commit ON segments(commit_sha, created_at);
