-- =====================================================================
-- Wastraq - GEOVISION EDGE INGESTION
--
-- Additive and idempotent. Safe to run repeatedly on a live database.
-- It creates new tables only. It never drops, alters or reseeds anything
-- that already exists, so the 16-property pilot lane, the survey layer,
-- the property master and collection_events are untouched.
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/geovision_integration.sql
--
-- ---------------------------------------------------------------------
-- Architectural boundary encoded here
-- ---------------------------------------------------------------------
-- GeoVision is an OBSERVATION source. It sees people, depth and RFID
-- taps. It cannot see a service zone, so it has no basis for saying
-- which property was served -- and nothing in this file lets it say so:
--
--   * No table here has a property_id column.
--   * No table here references properties, property_service_zones or
--     collection_events.
--   * Nothing here writes to collection_events or evidence. A serviced
--     property is decided by WASTRAQ's PostGIS association ladder
--     (backend/app/gis.py) and by nothing else.
--   * An RFID tap answers WHO and WHEN. It does not answer WHERE.
--   * Ambiguity is stored as ambiguity: binding_status AMBIGUOUS with
--     every candidate track kept, never collapsed to a best guess.
--
-- This is the ingestion floor for a later association engine, not the
-- association engine.
--
-- ---------------------------------------------------------------------
-- Rate
-- ---------------------------------------------------------------------
-- The edge publishes TRACK_UPDATE at ~5 Hz PER TRACK. Track updates are
-- therefore an UPSERT of current state keyed by
-- (source_id, session_id, track_id), not an append-only history: two
-- pickers for an eight-hour shift is ~290k events, and none of them is
-- interesting once the next one arrives. The raw envelope of every
-- accepted event is still kept in geovision_raw_events.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. geovision_raw_events - the audit floor and the dedup key
--
-- Written FIRST, before any normalised row, and it is what makes
-- ingestion idempotent: event_id is the primary key, so a redelivered
-- event loses the INSERT ... ON CONFLICT DO NOTHING race and no
-- downstream row is written for it.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_raw_events (
    event_id     TEXT PRIMARY KEY,                 -- UUID from the edge
    event_type   TEXT NOT NULL
                 CHECK (event_type IN ('TRACK_UPDATE','RFID_TAP',
                                       'WORKER_TRACK_BOUND',
                                       'EVIDENCE_READY','HEARTBEAT')),
    source_id    TEXT NOT NULL,                    -- GEOVISION-D455-01 ...
    session_id   TEXT,                             -- capture session; track ids
                                                   -- are only unique within one
    event_time   TIMESTAMPTZ NOT NULL,             -- the edge's clock, UTC
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB NOT NULL,                   -- exactly what arrived
    processed    BOOLEAN NOT NULL DEFAULT FALSE    -- normalised row written
);

COMMENT ON TABLE geovision_raw_events IS
  'Every accepted GeoVision edge event, verbatim. event_id is the idempotency key.';
COMMENT ON COLUMN geovision_raw_events.event_time IS
  'Sender clock (UTC). Two machines are involved; compare with received_at for skew.';

CREATE INDEX IF NOT EXISTS idx_gv_raw_received
    ON geovision_raw_events (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_gv_raw_type_time
    ON geovision_raw_events (event_type, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_gv_raw_source
    ON geovision_raw_events (source_id, received_at DESC);


-- ---------------------------------------------------------------------
-- 2. geovision_track_updates - CURRENT state of one tracked person
--
-- Camera-relative metres, RealSense convention: x right, y down,
-- z forward along the optical axis. No world position is implied and
-- none may be inferred: there is no vehicle pose and no extrinsic
-- calibration behind these numbers.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_track_updates (
    source_id           TEXT NOT NULL,
    -- '' rather than NULL: this is part of the primary key, and NULLs in a
    -- key silently stop ON CONFLICT from ever matching.
    session_id          TEXT NOT NULL DEFAULT '',
    track_id            INTEGER NOT NULL,

    first_seen_at       TIMESTAMPTZ NOT NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL,
    last_event_id       TEXT NOT NULL,
    observation_count   BIGINT NOT NULL DEFAULT 1,

    confidence          DOUBLE PRECISION,          -- detector confidence
    bbox                JSONB,                     -- {x1,y1,x2,y2} pixels

    depth_m             DOUBLE PRECISION,
    camera_x_m          DOUBLE PRECISION,
    camera_y_m          DOUBLE PRECISION,
    camera_z_m          DOUBLE PRECISION,
    relative_x_m        DOUBLE PRECISION,
    relative_forward_m  DOUBLE PRECISION,
    depth_valid         BOOLEAN NOT NULL DEFAULT FALSE,
    -- NO_DEPTH_FRAME | NO_INTRINSICS | NO_DEPTH_SCALE | NO_VALID_SAMPLES
    -- | DEPROJECTION_FAILED | OK
    depth_status        TEXT,

    -- Identity comes from RFID, never from appearance.
    is_authorized_picker BOOLEAN NOT NULL DEFAULT FALSE,
    collector_id        TEXT,
    identity_confidence DOUBLE PRECISION,

    -- The phone fix that happened to be current, kept as its own object.
    -- It is NOT fused with the camera observation: different device,
    -- different error model, and an 8 m fix must never be mistaken for a
    -- 3 cm depth reading.
    gps                 JSONB,

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, session_id, track_id)
);

COMMENT ON TABLE geovision_track_updates IS
  'Latest observation per camera track. Camera-relative metres only - no world position, no property.';
COMMENT ON COLUMN geovision_track_updates.gps IS
  'Coarse phone fix carried alongside the observation. Not fused, not authoritative, never used for association.';

CREATE INDEX IF NOT EXISTS idx_gv_track_last_seen
    ON geovision_track_updates (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_gv_track_collector
    ON geovision_track_updates (collector_id)
    WHERE collector_id IS NOT NULL;


-- ---------------------------------------------------------------------
-- 3. geovision_rfid_taps - WHO and WHEN, never WHERE
--
-- binding_status is the edge's honest account of whether it could
-- attribute the tap to exactly one camera track:
--   BOUND                   one track in the reader zone; track_id set
--   AMBIGUOUS               several; track_id NULL, candidates listed
--   NO_TRACK_IN_READER_ZONE nobody visible at the reader
--   NO_TRACK_DATA           tracker had nothing to offer
--   UNKNOWN_RFID            card not on the roster
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_rfid_taps (
    event_id           TEXT PRIMARY KEY
                       REFERENCES geovision_raw_events(event_id)
                       ON UPDATE CASCADE ON DELETE CASCADE,
    source_id          TEXT NOT NULL,
    session_id         TEXT,
    event_time         TIMESTAMPTZ NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    rfid_uid           TEXT NOT NULL,
    collector_id       TEXT,                       -- edge's roster name
    track_id           INTEGER,                    -- NULL unless BOUND
    binding_status     TEXT NOT NULL
                       CHECK (binding_status IN ('BOUND','AMBIGUOUS',
                                                 'NO_TRACK_IN_READER_ZONE',
                                                 'NO_TRACK_DATA','UNKNOWN_RFID')),
    binding_confidence DOUBLE PRECISION,
    candidate_track_ids INTEGER[] NOT NULL DEFAULT '{}',
    reason             TEXT
);

COMMENT ON TABLE geovision_rfid_taps IS
  'One RFID tap. Establishes identity and time only - a tap says nothing about which property was served.';
COMMENT ON COLUMN geovision_rfid_taps.candidate_track_ids IS
  'Every track that could have been the tapper. Kept whole: ambiguity is preserved, not resolved by a guess.';

CREATE INDEX IF NOT EXISTS idx_gv_rfid_time
    ON geovision_rfid_taps (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_gv_rfid_uid
    ON geovision_rfid_taps (rfid_uid, event_time DESC);


-- ---------------------------------------------------------------------
-- 4. geovision_worker_bindings - collector <-> camera track, per session
--
-- Append-only. A binding is a fact that happened at a time; the current
-- binding is the newest row, not a mutated one.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_worker_bindings (
    event_id       TEXT PRIMARY KEY
                   REFERENCES geovision_raw_events(event_id)
                   ON UPDATE CASCADE ON DELETE CASCADE,
    source_id      TEXT NOT NULL,
    session_id     TEXT,
    event_time     TIMESTAMPTZ NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    collector_id   TEXT NOT NULL,
    rfid_uid       TEXT,
    track_id       INTEGER NOT NULL,
    confidence     DOUBLE PRECISION,
    -- The RFID_TAP this binding came from. Not a foreign key on purpose:
    -- events can arrive out of order after a retry, and a binding is worth
    -- keeping even if its tap is still in the queue.
    rfid_event_id  TEXT
);

COMMENT ON TABLE geovision_worker_bindings IS
  'collector_id <-> track_id for one capture session. Track ids are only meaningful within a session_id.';

CREATE INDEX IF NOT EXISTS idx_gv_binding_time
    ON geovision_worker_bindings (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_gv_binding_collector
    ON geovision_worker_bindings (collector_id, event_time DESC);


-- ---------------------------------------------------------------------
-- 5. geovision_evidence_clips - a REFERENCE to a clip, never the bytes
--
-- The MP4 stays on the GeoVision machine. file_path is a Windows path on
-- that machine and is only meaningful there. Deliberately NOT written
-- into the `evidence` table: that table's rows hang off a
-- collection_event, and no collection event exists until WASTRAQ has
-- associated a property.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_evidence_clips (
    event_id       TEXT PRIMARY KEY
                   REFERENCES geovision_raw_events(event_id)
                   ON UPDATE CASCADE ON DELETE CASCADE,
    source_id      TEXT NOT NULL,
    session_id     TEXT,
    event_time     TIMESTAMPTZ NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    clip_id        TEXT NOT NULL,
    file_path      TEXT NOT NULL,                  -- remote path on the edge
    clip_start     TIMESTAMPTZ,
    clip_end       TIMESTAMPTZ,
    frame_count    INTEGER,
    track_id       INTEGER,
    rfid_event_id  TEXT,
    fetched        BOOLEAN NOT NULL DEFAULT FALSE  -- has a human pulled it yet
);

COMMENT ON TABLE geovision_evidence_clips IS
  'Pointer to a clip held on the GeoVision machine. Not linked to a collection event - no property has been decided.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_gv_clip
    ON geovision_evidence_clips (source_id, clip_id);
CREATE INDEX IF NOT EXISTS idx_gv_clip_time
    ON geovision_evidence_clips (event_time DESC);


-- ---------------------------------------------------------------------
-- 6. geovision_devices - liveness, so "no pickers" reads differently
--    from "no GeoVision"
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_devices (
    source_id           TEXT PRIMARY KEY,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_type     TEXT,
    last_event_at       TIMESTAMPTZ,
    last_heartbeat_at   TIMESTAMPTZ,
    last_session_id     TEXT,
    -- The edge's own /integration/status payload, as sent.
    last_status         JSONB,
    events_received     BIGINT NOT NULL DEFAULT 0,
    duplicates_ignored  BIGINT NOT NULL DEFAULT 0
);

COMMENT ON TABLE geovision_devices IS
  'One row per edge source_id. Liveness and last reported self-diagnosis.';


-- ---------------------------------------------------------------------
-- 7. geovision_ingest_summary - what the status endpoint reads
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW geovision_ingest_summary AS
SELECT
    event_type,
    count(*)             AS events,
    max(event_time)      AS last_event_time,
    max(received_at)     AS last_received_at,
    count(*) FILTER (WHERE NOT processed) AS unprocessed
FROM geovision_raw_events
GROUP BY event_type;

COMMIT;

-- ---------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------
--   \dt geovision_*
--   SELECT * FROM geovision_ingest_summary;
--   SELECT source_id, last_seen_at, events_received FROM geovision_devices;
