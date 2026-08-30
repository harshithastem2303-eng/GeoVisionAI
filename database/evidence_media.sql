-- ---------------------------------------------------------------------
-- evidence_media.sql - STEP 4A: make GeoVision clips reachable from the Mac
--
-- Run AFTER schema.sql, geovision_integration.sql and episodes.sql.
-- Idempotent: safe to re-run.
--
--   psql -d wastraq_demo -f database/evidence_media.sql
--
-- The problem this fixes
-- ----------------------
-- An EVIDENCE_READY event carries `file_path` - a path on the WINDOWS
-- machine. Until now `episodes.store.attach_clip()` copied that string
-- straight into `evidence.file_path`, where the dashboard printed it as if
-- it were a file. It is not a file here. A browser on the Mac cannot open
-- `C:\GeoVision\clips\x.mp4`, and a UI that shows it as "the evidence"
-- claims something WASTRAQ cannot produce.
--
-- The rule this migration encodes: the Windows path is PROVENANCE, kept in
-- geovision_evidence_clips where it is documented as remote. It never
-- becomes a browser-facing location. What the operator plays is always a
-- Mac-local byte stream, served by the Mac, from a file the Mac holds.
-- ---------------------------------------------------------------------

BEGIN;

-- ---------------------------------------------------------------------
-- 1. geovision_evidence_clips: how to GET the bytes, and whether we have
-- ---------------------------------------------------------------------
-- file_path stays exactly as it was: the remote path, for the audit trail.
-- Everything added here is about retrieval, which is a separate concern
-- from provenance and is allowed to fail without invalidating the record.
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS file_url      TEXT;      -- http(s) URL on the edge
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS content_type  TEXT NOT NULL DEFAULT 'video/mp4';
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS size_bytes    BIGINT;    -- as declared by the edge
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS sha256        TEXT;      -- as declared by the edge

-- Retrieval state. local_path is relative to the Mac evidence root, never
-- absolute: an absolute path in the database is a path that stops being
-- true the moment the repo moves, and is one string-concatenation away
-- from being a traversal primitive.
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS local_path    TEXT;
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS local_bytes   BIGINT;
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS fetch_status  TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS fetch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS fetch_error   TEXT;
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS last_fetch_at TIMESTAMPTZ;

-- PENDING     announced, not pulled yet
-- FETCHING    a pull is in flight (single process; advisory)
-- STORED      the bytes are on this Mac at local_path
-- UNAVAILABLE the edge was asked and could not deliver - retryable
-- SKIPPED     fetching is switched off for this deployment
DO $$
BEGIN
    ALTER TABLE geovision_evidence_clips
        ADD CONSTRAINT chk_gv_clip_fetch_status
        CHECK (fetch_status IN ('PENDING','FETCHING','STORED','UNAVAILABLE','SKIPPED'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN geovision_evidence_clips.file_path IS
  'Path on the GeoVision (Windows) machine. Provenance only - never served to a browser.';
COMMENT ON COLUMN geovision_evidence_clips.file_url IS
  'HTTP(S) URL on the edge that returns the clip bytes. Null means derive it from GEOVISION_EDGE_BASE_URL.';
COMMENT ON COLUMN geovision_evidence_clips.local_path IS
  'Path of the fetched copy, RELATIVE to the Mac evidence root. Null until fetch_status = STORED.';

CREATE INDEX IF NOT EXISTS idx_gv_clip_fetch_status
    ON geovision_evidence_clips (fetch_status)
    WHERE fetch_status <> 'STORED';

-- Back-fill: anything already marked fetched really is on disk somewhere,
-- but we do not know where, so it is PENDING like everything else. The
-- column is left alone rather than guessed at.
UPDATE geovision_evidence_clips
   SET fetch_status = 'PENDING'
 WHERE fetch_status IS NULL;

-- ---------------------------------------------------------------------
-- 2. evidence.clip_event_id - one clip, at most one evidence row
-- ---------------------------------------------------------------------
-- This is the idempotency the transport-level event_id dedup cannot give
-- us. The edge may legitimately re-announce the same clip under a fresh
-- envelope (a retry after our ack was lost, a restarted publisher). The
-- raw-event table dedups the DELIVERY; this index dedups the CLIP. Without
-- it, a second announcement produces a second VIDEO_CLIP row against the
-- same collection event and the operator sees the same footage twice.
--
-- Deliberately a UNIQUE INDEX rather than application logic: two workers,
-- or a retry racing the sweeper, cannot both pass a read-then-write check.
ALTER TABLE evidence
    ADD COLUMN IF NOT EXISTS clip_event_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_clip_event
    ON evidence (clip_event_id)
    WHERE clip_event_id IS NOT NULL;

COMMENT ON COLUMN evidence.clip_event_id IS
  'geovision_evidence_clips.event_id this row was created from. Unique: one clip -> one evidence row.';

-- ---------------------------------------------------------------------
-- 3. v_evidence_media - what the API reads
-- ---------------------------------------------------------------------
-- One place that answers "is there something an operator can actually
-- play, and if not, why not". The API adds the URL; the database decides
-- availability, because availability is a fact about stored rows.
CREATE OR REPLACE VIEW v_evidence_media AS
SELECT
    e.evidence_id,
    e.event_id,
    e.evidence_type,
    e.file_path,
    e.captured_at,
    e.verified,
    e.clip_event_id,
    c.source_id        AS clip_source_id,
    c.clip_id,
    c.file_path        AS remote_file_path,
    c.file_url         AS remote_file_url,
    c.content_type,
    c.local_path,
    c.local_bytes,
    c.fetch_status,
    c.fetch_attempts,
    c.fetch_error,
    c.last_fetch_at,
    c.episode_id       AS clip_episode_id,
    CASE
        WHEN e.clip_event_id IS NULL           THEN 'LOCAL'
        WHEN c.fetch_status  = 'STORED'        THEN 'STORED'
        WHEN c.fetch_status  = 'UNAVAILABLE'   THEN 'UNAVAILABLE'
        WHEN c.event_id      IS NULL           THEN 'ORPHANED'
        ELSE 'PENDING'
    END AS media_state,
    -- STEP 4C. Appended, never inserted: CREATE OR REPLACE VIEW may only
    -- add columns at the end, so a database that already has the 4A view
    -- takes this migration as a replace rather than needing a DROP.
    --
    -- These are what the operator is shown INSTEAD of the Windows path:
    -- when the clip was recorded, how long it ran, and which track it
    -- belongs to. All of it describes the footage; none of it describes a
    -- filesystem, so none of it can leak one into the browser.
    c.clip_start,
    c.clip_end,
    c.frame_count,
    c.track_id         AS clip_track_id,
    c.event_time       AS clip_event_time
FROM evidence e
LEFT JOIN geovision_evidence_clips c ON c.event_id = e.clip_event_id;

COMMENT ON VIEW v_evidence_media IS
  'Evidence rows joined to the edge clip they came from, with retrieval state. media_state LOCAL means the row is not a GeoVision clip.';

COMMIT;
