-- =====================================================================
-- Remove the rows one STEP 2 test run created, and nothing else.
--
--   psql wastraq_demo -v source="'GEOVISION-STEP2-05ef9728'" \
--        -f database/cleanup_step2_test.sql
--
-- The source_id is printed at the end of every
-- scripts/test_step2_episode_flow.py run. It is generated per run, so this
-- script can only ever reach that one run's rows.
--
-- What it deletes
-- ---------------
--   evidence rows attached to that run's collection events
--   collection_events written by that run's episodes
--   collection_episodes created by that run
--   geovision_track_updates / raw events / bindings from that source
--
-- What it CANNOT touch, by construction
-- -------------------------------------
--   properties, property_entrances, property_frontages,
--   property_service_zones, property_photos, pickers - no statement here
--   names them. The surveyed lane is not test data and is never cleaned up.
--
-- It runs in ONE transaction and prints what it removed, so a wrong
-- source_id costs nothing: the counts come back zero and you can roll back.
-- =====================================================================

\if :{?source}
\else
  \echo 'ERROR: pass the run''s source_id, e.g.'
  \echo '  psql wastraq_demo -v source="''GEOVISION-STEP2-abc12345''" -f database/cleanup_step2_test.sql'
  \quit
\endif

BEGIN;

\echo 'Episodes that will be removed:'
SELECT episode_id, property_id, state, segregation_status, collection_event_id
  FROM collection_episodes
 WHERE source_id = :source
 ORDER BY started_at;

CREATE TEMP TABLE _step2_episodes ON COMMIT DROP AS
SELECT episode_id, collection_event_id
  FROM collection_episodes
 WHERE source_id = :source;

-- Evidence first: it is the child of collection_events.
WITH gone AS (
    DELETE FROM evidence
     WHERE event_id IN (SELECT collection_event_id FROM _step2_episodes
                         WHERE collection_event_id IS NOT NULL)
    RETURNING 1
) SELECT count(*) AS evidence_deleted FROM gone;

-- Break the episode -> event reference before deleting the event, so the
-- delete cannot trip a foreign key in either direction.
UPDATE collection_episodes
   SET collection_event_id = NULL
 WHERE episode_id IN (SELECT episode_id FROM _step2_episodes);

WITH gone AS (
    DELETE FROM collection_events
     WHERE event_id IN (SELECT collection_event_id FROM _step2_episodes
                         WHERE collection_event_id IS NOT NULL)
    RETURNING 1
) SELECT count(*) AS collection_events_deleted FROM gone;

WITH gone AS (
    DELETE FROM collection_episodes WHERE source_id = :source RETURNING 1
) SELECT count(*) AS episodes_deleted FROM gone;

-- Children of geovision_raw_events go before it: rfid taps, bindings,
-- evidence clips and non-segregation triggers all carry an FK to the raw
-- envelope they came from.
WITH gone AS (
    DELETE FROM geovision_track_updates WHERE source_id = :source RETURNING 1
) SELECT count(*) AS track_rows_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_non_segregation_triggers WHERE source_id = :source
    RETURNING 1
) SELECT count(*) AS triggers_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_evidence_clips WHERE source_id = :source RETURNING 1
) SELECT count(*) AS clips_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_worker_bindings WHERE source_id = :source RETURNING 1
) SELECT count(*) AS bindings_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_rfid_taps WHERE source_id = :source RETURNING 1
) SELECT count(*) AS rfid_taps_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_raw_events WHERE source_id = :source RETURNING 1
) SELECT count(*) AS raw_events_deleted FROM gone;

WITH gone AS (
    DELETE FROM geovision_devices WHERE source_id = :source RETURNING 1
) SELECT count(*) AS device_rows_deleted FROM gone;

-- The survey, untouched. These counts are the point of printing them.
\echo 'The surveyed lane after cleanup (must be unchanged):'
SELECT (SELECT count(*) FROM properties)             AS properties,
       (SELECT count(*) FROM property_service_zones) AS service_zones,
       (SELECT count(*) FROM property_entrances)     AS entrances,
       (SELECT count(*) FROM property_frontages)     AS frontages;

COMMIT;
