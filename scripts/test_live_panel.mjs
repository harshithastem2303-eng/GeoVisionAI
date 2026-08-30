// The Live collection-state panel, rendered without a browser.
//
//     node scripts/test_live_panel.mjs
//
// The backend test (scripts/test_live_panel.py) proves the payload. This
// proves the SECTION: that each of the states the demo can land in renders
// something an operator can read, that the play action carries an event id
// into the EXISTING evidence modal rather than a second player, and that
// nothing rendered anywhere is a Windows path.
//
// The functions are lifted out of dashboard.html itself, so this cannot
// drift from the page the way a re-typed copy would.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(HERE, '..', 'backend', 'app', 'static', 'dashboard.html');
const src = fs.readFileSync(HTML, 'utf8');
const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

const start = scripts.indexOf('const LIVE_POLL_MS');
const end = scripts.indexOf('let liveBusy');
if (start < 0 || end < 0 || end < start) {
  console.error('!! could not locate the live-panel helpers in dashboard.html');
  process.exit(2);
}
const helpers = scripts.slice(start, end);

const WQ = {
  esc: s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  chip: (k, l) => '<span class="chip good"><span class="dot"></span>' + (l || k) + '</span>',
};

const fn = new Function('WQ', helpers +
  '\nreturn { LIVE_POLL_MS, liveTracking, liveBinding, liveEpisode, liveEvidence, liveHealth };');
const P = fn(WQ);

let pass = 0, fail = 0;
function ok(claim, cond, detail) {
  if (cond) { pass++; console.log('  ok   ' + claim); }
  else { fail++; console.log('  FAIL ' + claim + (detail ? '  <- ' + detail : '')); }
}
function head(t) { console.log('\n' + t + '\n' + '-'.repeat(t.length)); }
const has = (h, ...bits) => bits.every(b => h.includes(b));

// --- fixtures: the demo's real shapes ---------------------------------------
const LIVE = {
  tracking: {
    available: true, track_id: 214, collector_id: 'GC001', collector_name: 'Ramesh',
    rfid_uid: '69F04D05', depth_m: 2.413, depth_valid: true, depth_status: 'OK',
    authorized: true, authorization_state: 'AUTHORIZED', identity_confidence: 0.88,
    confidence: 0.91, source: 'GEOVISION_EDGE', track_count: 2
  },
  binding: {
    bound: true, collector_id: 'GC001', collector_name: 'Ramesh', track_id: 214,
    rfid_uid: '69F04D05', locked: true, identity_confidence: 0.88,
    selection_rule: 'DEPTH_IN_ZONE', known_to_wastraq: true, known_to_geovision: true
  },
  episode: {
    state: 'ACTIVE', episode_id: 'EP-0007', property_id: 'PROP-005', track_id: 214,
    segregation_status: 'SEGREGATED', association_status: 'AUTO_ASSOCIATED',
    association_confidence: 0.953, observations: 37, house_number: '5', candidate: null
  },
  evidence: {
    event_id: 'EVENT-0031', evidence_count: 3, playable_count: 2, pending_count: 1,
    unavailable_count: 0, placeholder_count: 1, media_status: 'AVAILABLE',
    playable: true, from_episode: true, evidence_id: 'EV-1'
  },
  health: {
    geovision_connected: true, geovision_error: null, episode_engine_enabled: true,
    camera_configured: true, mirror_enabled: true, ingest_track_count: 2,
    db_active_episodes: 1
  }
};

const EMPTY = {
  tracking: { available: false, authorization_state: 'NO_PICKER', track_count: 0 },
  binding: { bound: false, edge_binding_count: 0 },
  episode: { state: 'NONE', property_id: null, candidate: null },
  evidence: { event_id: null, media_status: 'NONE', evidence_count: 0, playable_count: 0 },
  health: {
    geovision_connected: false, geovision_error: 'URLError: Connection refused',
    episode_engine_enabled: true, camera_configured: false, mirror_enabled: false,
    ingest_track_count: 0, db_active_episodes: 0
  }
};

// --- 1: the live demo state renders every field asked for --------------------
head('1  the live state shows tracking, binding, episode, evidence, health');
const t = P.liveTracking(LIVE.tracking);
ok('the authorised track id is shown', has(t, '#214', 'Authorized picker'));
ok('the collector is shown by name and id', has(t, 'Ramesh', 'GC001'));
ok('the RFID uid is shown', has(t, '69F04D05'));
ok('depth is shown in metres', has(t, '2.413 m'));

const b = P.liveBinding(LIVE.binding);
ok('the binding reads BOUND', has(b, 'Bound'));
ok('collector -> track is shown as a relation', has(b, 'GC001 → #214'));
ok('the lock state is shown', has(b, 'Locked'));
ok('identity confidence is shown', has(b, '0.88'));

const e = P.liveEpisode(LIVE.episode);
ok('the episode id is shown', has(e, 'EP-0007'));
ok('the property id is shown', has(e, 'PROP-005'));
ok('the episode state is shown', has(e, 'Episode active'));
ok('the segregation status is shown', has(e, 'SEGREGATED'));
ok('the association status and confidence are shown',
   has(e, 'AUTO_ASSOCIATED', '0.953'));
ok('the observation count is shown', has(e, '37'));

const h = P.liveHealth(LIVE.health);
ok('health says GeoVision connected', has(h, 'GeoVision connected'));
ok('health says the episode engine is on', has(h, 'Episode engine on'));
ok('health says the camera is configured', has(h, 'Camera configured'));

// --- 2: evidence reuses the existing modal ----------------------------------
head('2  evidence counts, and the ▶ action that opens the EXISTING modal');
const ev = P.liveEvidence(LIVE.evidence);
ok('the evidence count is shown', has(ev, 'EVENT-0031', '>3<'));
ok('the playable clip count is shown', has(ev, '>2<'));
ok('an available clip reads AVAILABLE', has(ev, 'Clip available'));
ok('an available clip offers ▶ Play clip', has(ev, '▶ Play clip'));
ok('the action carries the EVENT id, which is what openEvidence takes',
   /id="livePlay"[^>]*data-ev="EVENT-0031"/.test(ev) ||
   /data-ev="EVENT-0031"[^>]*id="livePlay"/.test(ev), ev);
ok('the panel builds no <video> of its own', !/<video/i.test(ev));
ok('the panel builds no media URL of its own', !/\/media/.test(ev));
ok('the whole panel builds no second player',
   !/<video/i.test(t + b + e + ev + h));
// Counted on the emitted markup, not on the comments that describe it:
// dashboard.html mentions <video> three times in prose and builds it once.
ok('the only playback route in the file is the evidence modal',
   (scripts.match(/'<video/g) || []).length === 1,
   String((scripts.match(/'<video/g) || []).length));
ok('the play button is wired to openEvidence and nothing else',
   /play\.addEventListener\('click', \(\) => openEvidence\(/.test(helpers +
     scripts.slice(scripts.indexOf('function renderLive'))));

const pending = P.liveEvidence({ ...LIVE.evidence, media_status: 'PENDING',
  playable: false, playable_count: 0, pending_count: 1, evidence_id: null });
ok('a clip being fetched reads "Clip pending"', has(pending, 'Clip pending'));
ok('a pending clip offers no play action', !pending.includes('▶ Play clip'));

const gone = P.liveEvidence({ ...LIVE.evidence, media_status: 'UNAVAILABLE',
  playable: false, playable_count: 0, unavailable_count: 1, evidence_id: null });
ok('an undeliverable clip reads UNAVAILABLE', has(gone, 'Clip unavailable'));
ok('an undeliverable clip shows a safe error state, not a broken player',
   has(gone, 'err-box', 'could not deliver') && !/<video/i.test(gone));
ok('an undeliverable clip still lets the operator open the event and retry',
   has(gone, 'Open evidence', 'data-ev="EVENT-0031"'));

const nothing = P.liveEvidence(EMPTY.evidence);
ok('no event yet is a state, not an empty box', has(nothing, 'No evidence yet'));

// --- 3: every degraded state renders -----------------------------------------
head('3  no picker, no binding, no episode, GeoVision offline');
const t0 = P.liveTracking(EMPTY.tracking);
ok('no picker in frame says so', has(t0, 'No active picker in frame'));
const b0 = P.liveBinding(EMPTY.binding);
ok('no binding says so', has(b0, 'Not bound'));
ok('no binding tells the operator what to do', has(b0, 'Tap the RFID card'));
const e0 = P.liveEpisode(EMPTY.episode);
ok('no episode says so', has(e0, 'No active episode'));
const h0 = P.liveHealth(EMPTY.health);
ok('GeoVision offline says disconnected', has(h0, 'GeoVision disconnected'));
ok('GeoVision offline carries the reason', has(h0, 'Connection refused'));
ok('an unset camera pose is visible', has(h0, 'Camera pose unset'));

const dwelling = P.liveEpisode({ state: 'NONE', property_id: null,
  candidate: { property_id: 'PROP-005', dwell_s: 1.8, dwell_required_s: 3 } });
ok('dwelling before the episode opens shows the property',
   has(dwelling, 'PROP-005', '1.8 s of 3 s'));

const closed = P.liveEpisode({ ...LIVE.episode, state: 'CLOSED',
  segregation_status: 'NOT_SEGREGATED' });
ok('a closed episode reads CLOSED', has(closed, 'Episode closed'));
ok('a closed NOT_SEGREGATED episode keeps its verdict on screen',
   has(closed, 'NOT_SEGREGATED'));

const ingest = P.liveTracking({ ...LIVE.tracking, source: 'WASTRAQ_INGEST' });
ok('a track that came from WASTRAQ ingest is labelled as such',
   has(ingest, 'from WASTRAQ ingest'));

// --- 4: nothing rendered is a path, and the page is not redesigned ------------
head('4  no filesystem path, and the existing dashboard is left alone');
const everything = [t, b, e, ev, h, t0, b0, e0, h0, pending, gone, nothing,
                    dwelling, closed, ingest].join('\n');
ok('no drive-letter path is rendered', !/[A-Za-z]:\\/.test(everything));
ok('no UNC path is rendered', !/\\\\[A-Za-z]/.test(everything));
ok('no file:// url is rendered', !/file:\/\//.test(everything));
ok('nothing renders a Windows host or user directory',
   !/Users\\|OneDrive|C:\//i.test(everything));

const poisoned = P.liveTracking({ ...LIVE.tracking,
  collector_name: '<img src=x onerror=alert(1)>' });
ok('edge-supplied strings are escaped, not injected',
   poisoned.includes('&lt;img') && !poisoned.includes('<img'));

ok('the panel polls at 1-2 s', P.LIVE_POLL_MS >= 1000 && P.LIVE_POLL_MS <= 2000,
   String(P.LIVE_POLL_MS));
ok('the existing 10 s operations auto-refresh is untouched',
   scripts.includes('WQ.autoRefresh(refresh, 10)'));
ok('the panel has its own poll and does not ride the operations refresh',
   scripts.includes('setInterval(refreshLive, LIVE_POLL_MS)') &&
   !/async function refresh\(\)[\s\S]{0,900}refreshLive\(/.test(scripts));
ok('a failing live poll cannot throw into the page',
   /async function refreshLive\(\)[\s\S]*?catch \(e\)/.test(scripts));
ok('the map, feed, drawer and evidence modal are still wired',
   ['function initMap', 'function renderFeed', 'function openProperty',
    'async function openEvidence', "WQ.drawer('pd')", "WQ.modal('evm')"]
     .every(x => scripts.includes(x)));
ok('the panel reads one endpoint, and it is the Mac-side proxy',
   scripts.includes("WQ.api('/live/state')") &&
   !/10\.235\.18\.118/.test(src));

console.log('\n' + '='.repeat(60));
console.log(pass + ' passed, ' + fail + ' failed');
console.log('='.repeat(60));
process.exit(fail ? 1 : 0);
