CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, route TEXT NOT NULL, created_at REAL NOT NULL,
 commit_sha TEXT NOT NULL, labels TEXT NOT NULL, manifest TEXT NOT NULL,
 upload_status TEXT NOT NULL DEFAULT 'uploading', review_status TEXT NOT NULL DEFAULT 'new',
 review_note TEXT NOT NULL DEFAULT '', fix_commit TEXT NOT NULL DEFAULT '',
 received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS events_review ON events(review_status, created_at);
CREATE TABLE IF NOT EXISTS event_labels (
 event_id TEXT NOT NULL REFERENCES events(id), label TEXT NOT NULL,
 PRIMARY KEY(event_id, label)
);
CREATE INDEX IF NOT EXISTS labels_lookup ON event_labels(label, event_id);
