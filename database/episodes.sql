-- =====================================================================
-- Wastraq - COLLECTION EPISODES + NON-SEGREGATION TRIGGERS
--
-- Additive and idempotent. Safe to run repeatedly on a live database.
-- It creates new tables, adds nullable columns and widens one CHECK
-- constraint. It never drops, alters or reseeds mapped geometry, so the
-- 16-property pilot lane, the survey layer, the property master and
-- existing collection_events are untouched.
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/episodes.sql
--
-- ---------------------------------------------------------------------
-- What an episode is
-- ---------------------------------------------------------------------
-- A COLLECTION EPISODE is WASTRAQ's own record of "a bound collector was
-- standing in exactly one mapped service zone, long enough to be servicing
-- that property". It is created by the episode engine
-- (backend/app/episodes/engine.py) from:
--
--     WORKER_TRACK_BOUND  ->  which camera track is the collector
--     TRACK_UPDATE        ->  where that track is, in camera metres
--     fixed-camera transform + PostGIS service zones -> which property
--     dwell                                          -> for how long
--
-- The property on an episode comes from ST_Within against
-- property_service_zones and from nothing else. GeoVision never supplies
-- one, cannot supply one, and is refused if it tries
-- (integrations/schemas.py FORBIDDEN_PROPERTY_FIELDS).
--
-- ---------------------------------------------------------------------
-- Why the Windows mirror is not stored as authority
-- ---------------------------------------------------------------------
-- GeoVision keeps an in-memory MIRROR of the active episode so a second
-- RFID tap has something to point at. That mirror carries episode_id,
-- track_id and association_status - never a property. mirror_status below
-- records only whether we managed to tell Windows about the episode. A
-- failed mirror is a degraded demo, never a corrupted record: the episode
-- stays valid and closes SEGREGATED on its own.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 0. Prerequisites
--
-- This file extends the GeoVision ingestion tables. Applying it to a
-- database that has never seen geovision_integration.sql would fail
-- halfway through with an opaque "relation does not exist"; say so
-- plainly and stop before anything is written instead.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.geovision_raw_events') IS NULL THEN
        RAISE EXCEPTION
            'Run database/geovision_integration.sql before database/episodes.sql';
    END IF;
    IF to_regclass('public.properties') IS NULL
       OR to_regclass('public.collection_events') IS NULL THEN
        RAISE EXCEPTION
            'Run database/schema.sql (and the lane seed) before database/episodes.sql';
    END IF;
END $$;


-- ---------------------------------------------------------------------
-- 1. The sixth GeoVision event type
--
-- geovision_raw_events.event_type is a CHECK, not an enum, so widening it
-- is a drop + add. Guarded so re-running is a no-op.
-- ---------------------------------------------------------------------
ALTER TABLE geovision_raw_events
    DROP CONSTRAINT IF EXISTS geovision_raw_events_event_type_check;

ALTER TABLE geovision_raw_events
    ADD CONSTRAINT geovision_raw_events_event_type_check
    CHECK (event_type IN ('TRACK_UPDATE','RFID_TAP','WORKER_TRACK_BOUND',
                          'EVIDENCE_READY','HEARTBEAT',
                          'NON_SEGREGATION_TRIGGER'));


-- ---------------------------------------------------------------------
-- 2. collection_episodes - WASTRAQ's authoritative episode record
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_episodes (
    episode_id             TEXT PRIMARY KEY,              -- E-001, E-002 ...

    -- Decided by PostGIS. NOT NULL: an episode without a property is not
    -- an episode, it is an ambiguous observation, and those are refused
    -- by the engine rather than stored as a half-association.
    property_id            TEXT NOT NULL
                           REFERENCES properties(property_id)
                           ON UPDATE CASCADE ON DELETE RESTRICT,

    -- Identity, from the RFID binding on the edge.
    collector_id           TEXT,                          -- edge roster name
    picker_id              TEXT REFERENCES pickers(picker_id)
                           ON UPDATE CASCADE ON DELETE SET NULL,

    -- Which camera, which capture session, which track. Track ids are only
    -- unique inside one session_id.
    source_id              TEXT NOT NULL,
    session_id             TEXT NOT NULL DEFAULT '',
    track_id               INTEGER NOT NULL,

    association_status     TEXT NOT NULL DEFAULT 'AUTO_ASSOCIATED'
                           CHECK (association_status IN ('AUTO_ASSOCIATED','REVIEW')),
    association_confidence DOUBLE PRECISION,
    association_method     TEXT,                          -- ST_WITHIN_SERVICE_ZONE ...

    started_at             TIMESTAMPTZ NOT NULL,
    last_seen_at           TIMESTAMPTZ NOT NULL,
    ended_at               TIMESTAMPTZ,
    dwell_s                DOUBLE PRECISION,
    observations           INTEGER NOT NULL DEFAULT 0,

    state                  TEXT NOT NULL DEFAULT 'ACTIVE'
                           CHECK (state IN ('ACTIVE','CLOSED','ABORTED')),

    -- SEGREGATED unless an accepted NON_SEGREGATION_TRIGGER says otherwise.
    -- The default IS the product rule: the collector only acts on the
    -- exception.
    segregation_status     TEXT NOT NULL DEFAULT 'SEGREGATED'
                           CHECK (segregation_status IN ('SEGREGATED','NOT_SEGREGATED')),
    -- The trigger that flipped it. Also the semantic idempotency anchor.
    non_segregation_trigger_id TEXT,
    non_segregated_at      TIMESTAMPTZ,

    -- Written when the episode closes. No FK: collection_events already
    -- carries episode_id the other way and a circular FK pair buys nothing
    -- but insert-ordering pain.
    collection_event_id    TEXT,

    -- Advisory only: did Windows accept the mirror of this episode.
    mirror_status          TEXT NOT NULL DEFAULT 'PENDING'
                           CHECK (mirror_status IN ('PENDING','DISABLED','MIRRORED',
                                                    'MIRROR_FAILED','REMOVED',
                                                    'REMOVE_FAILED')),
    mirror_error           TEXT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE collection_episodes IS
  'A bound collector dwelling in one mapped service zone. Property comes from PostGIS, never from GeoVision.';
COMMENT ON COLUMN collection_episodes.mirror_status IS
  'Whether Windows was told about this episode. Advisory: a mirror failure never changes the episode outcome.';

CREATE INDEX IF NOT EXISTS idx_episode_property   ON collection_episodes (property_id);
CREATE INDEX IF NOT EXISTS idx_episode_started    ON collection_episodes (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_episode_track      ON collection_episodes (source_id, session_id, track_id);
CREATE INDEX IF NOT EXISTS idx_episode_state      ON collection_episodes (state);

-- One live episode per collector. The engine enforces this in memory too;
-- this is the guard that survives a restart.
CREATE UNIQUE INDEX IF NOT EXISTS uq_episode_active_collector
    ON collection_episodes (collector_id)
    WHERE state = 'ACTIVE' AND collector_id IS NOT NULL;


-- ---------------------------------------------------------------------
-- 3. geovision_non_segregation_triggers - the sixth event, normalised
--
-- Note what is NOT here: no property_id, no service_zone_id, no
-- segregation_status. GeoVision raises a SIGNAL. Which property it lands
-- on is read through applied_episode_id, and that episode's property was
-- decided by PostGIS.
--
-- trigger_id is the PRIMARY KEY, which is the semantic idempotency the
-- transport-level event_id dedup cannot give us: the edge may re-announce
-- the same decision under a new event_id after a retry-queue restart.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geovision_non_segregation_triggers (
    trigger_id         TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL
                       REFERENCES geovision_raw_events(event_id)
                       ON UPDATE CASCADE ON DELETE CASCADE,
    source_id          TEXT NOT NULL,
    session_id         TEXT,
    event_time         TIMESTAMPTZ NOT NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- What GeoVision believes it is pointing at. Advisory: it is checked
    -- against WASTRAQ's own episode before anything is marked.
    claimed_episode_id TEXT,
    collector_id       TEXT,
    rfid_uid           TEXT,
    track_id           INTEGER,
    trigger_status     TEXT,
    edge_duplicate     BOOLEAN NOT NULL DEFAULT FALSE,
    rfid_event_id      TEXT,

    -- WASTRAQ's verdict on the signal.
    applied            BOOLEAN NOT NULL DEFAULT FALSE,
    applied_episode_id TEXT REFERENCES collection_episodes(episode_id)
                       ON UPDATE CASCADE ON DELETE SET NULL,
    resolution         TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (resolution IN ('PENDING','APPLIED','DUPLICATE',
                                             'UNKNOWN_EPISODE','EPISODE_NOT_ACTIONABLE',
                                             'IDENTITY_MISMATCH','EDGE_UNRESOLVED',
                                             'ENGINE_DISABLED','ERROR')),
    resolution_detail  TEXT,
    -- A signal we could not land is kept for a human, never silently
    -- dropped and never applied to a neighbouring house.
    needs_review       BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE geovision_non_segregation_triggers IS
  'A second RFID tap from a bound collector. A signal, not a verdict: it carries no property and no segregation status.';
COMMENT ON COLUMN geovision_non_segregation_triggers.claimed_episode_id IS
  'The episode id GeoVision echoed back. Verified against collection_episodes before use; a mismatch is preserved for review.';

CREATE INDEX IF NOT EXISTS idx_gv_trigger_time
    ON geovision_non_segregation_triggers (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_gv_trigger_episode
    ON geovision_non_segregation_triggers (claimed_episode_id);
CREATE INDEX IF NOT EXISTS idx_gv_trigger_review
    ON geovision_non_segregation_triggers (needs_review) WHERE needs_review;


-- ---------------------------------------------------------------------
-- 4. Linkage columns on existing tables (all nullable, all additive)
-- ---------------------------------------------------------------------
ALTER TABLE collection_events
    ADD COLUMN IF NOT EXISTS episode_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_episode
    ON collection_events (episode_id) WHERE episode_id IS NOT NULL;

-- Which episode a clip was attributed to, and the evidence row it became.
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS episode_id TEXT;
ALTER TABLE geovision_evidence_clips
    ADD COLUMN IF NOT EXISTS linked_evidence_id TEXT;
CREATE INDEX IF NOT EXISTS idx_gv_clip_episode
    ON geovision_evidence_clips (episode_id) WHERE episode_id IS NOT NULL;


-- ---------------------------------------------------------------------
-- 5. v_episode_summary - what the dashboard and /episodes read
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_episode_summary AS
SELECT
    ep.episode_id,
    ep.property_id,
    p.house_number,
    p.owner_name,
    p.formatted_address,
    p.route_id,
    ep.collector_id,
    ep.picker_id,
    pk.picker_name,
    ep.source_id,
    ep.session_id,
    ep.track_id,
    ep.association_status,
    ep.association_confidence,
    ep.association_method,
    ep.started_at,
    ep.last_seen_at,
    ep.ended_at,
    ep.dwell_s,
    ep.observations,
    ep.state,
    ep.segregation_status,
    ep.non_segregation_trigger_id,
    ep.collection_event_id,
    ep.mirror_status,
    (SELECT count(*) FROM evidence e WHERE e.event_id = ep.collection_event_id)
        AS evidence_count
FROM collection_episodes ep
JOIN properties p   ON p.property_id = ep.property_id
LEFT JOIN pickers pk ON pk.picker_id = ep.picker_id;

COMMIT;

-- ---------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------
--   \d collection_episodes
--   \d geovision_non_segregation_triggers
--   SELECT episode_id, property_id, state, segregation_status FROM collection_episodes ORDER BY started_at DESC LIMIT 10;
--   SELECT count(*) FROM properties;   -- must still be 16 on the pilot lane
--
-- Note: database/schema.sql DROPs properties CASCADE, which would take
-- collection_episodes with it. Re-running schema.sql therefore means
-- re-running geovision_integration.sql and this file afterwards. Neither
-- of those two drops anything, so the order is always:
--   schema.sql -> seed/lane -> geovision_integration.sql -> episodes.sql
