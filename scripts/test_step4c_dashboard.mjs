// STEP 4C - click-to-video, proved without a browser.
//
//     node scripts/test_step4c_dashboard.mjs
//
// The step is one sentence: clicking evidence on a NOT_SEGREGATED event
// plays the actual GeoVision clip. Three things have to be true for that
// sentence to hold, and each is checked here against the real functions
// lifted out of dashboard.html:
//
//   1  the feed offers an ACTION carrying the event id (not a decoration);
//   2  an AVAILABLE clip renders an HTML5 <video> pointing at the WASTRAQ
//      media url, wrapped in a loading state and an error state;
//   3  nothing rendered anywhere is a Windows path, and nothing rendered
//      is a demo placeholder dressed up as evidence.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(HERE, '..', 'backend', 'app', 'static', 'dashboard.html');

const src = fs.readFileSync(HTML, 'utf8');
const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const start = scripts.indexOf('const MEDIA_NOTE');
const end = scripts.indexOf('// Click-to-video.');
if (start < 0 || end < 0 || end < start) {
  console.error('!! could not locate the evidence-media helpers in dashboard.html');
  process.exit(2);
}
const helpers = scripts.slice(start, end);

const WQ = {
  esc: s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  num: n => String(n),
  chip: (s, l) => `<span class="chip good">${l || s}</span>`,
  dateOf: () => '2026-08-30',
  timeOf: t => (t ? String(t).slice(11, 19) : '—'),
  toast: () => {},
};

// --- a DOM small enough to hold in your head, real enough to wire events --
function el(tag, attrs) {
  const node = {
    tag, attrs: attrs || {}, children: [], listeners: {}, style: {},
    className: '', textContent: '', disabled: false, error: null, readyState: 0,
    getAttribute: k => (node.attrs[k] === undefined ? null : node.attrs[k]),
    setAttribute: (k, v) => { node.attrs[k] = v; },
    querySelector: sel => node.children.find(c => matches(c, sel)) || null,
    addEventListener: (ev, fn) => { (node.listeners[ev] ||= []).push(fn); },
    dispatchEvent: e => (node.listeners[e.type] || []).forEach(fn => fn(e)),
    appendChild: c => { node.children.push(c); return c; },
  };
  return node;
}
function matches(node, sel) {
  if (sel === 'video') return node.tag === 'video';
  if (sel.startsWith('.')) return (' ' + node.className + ' ').includes(' ' + sel.slice(1) + ' ');
  return false;
}
const document = {
  stages: [],
  querySelectorAll: sel => (sel.includes('ev-stage') ? document.stages : []),
  createElement: tag => el(tag),
};
global.Event = class { constructor(type) { this.type = type; } };

const M = new Function('WQ', 'document', 'global', helpers +
  'return { evidenceCard, evidencePlayer, evidenceAction, mediaFootnote,' +
  ' placeholderNote, sourceLabel, clipTiming, mediaErrorText,' +
  ' wireEvidenceMedia, wireEvidenceFetch };')(WQ, document, global);

let pass = 0, fail = 0;
const t = (name, cond, detail) => {
  cond ? pass++ : fail++;
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail ? `\n         ${detail}` : ''));
};

const WIN = 'C:\\GeoVision\\clips\\CLIP-77.mp4';
const clip = {
  evidence_id: 'EVID-042', evidence_type: 'NON_SEGREGATION_PROOF',
  captured_at: '2026-08-30T07:12:04Z', verified: false,
  media_status: 'AVAILABLE', media_kind: 'video', media_url: '/evidence/EVID-042/media',
  media_bytes: 1483920, media_content_type: 'video/mp4',
  source_ref: WIN, source_label: 'GeoVision GEOVISION-D455-01 · CLIP-3f2a1b0c9d8e',
  source_kind: 'GEOVISION_EDGE', is_placeholder: false,
  clip_id: 'CLIP-3f2a1b0c9d8e', clip_source_id: 'GEOVISION-D455-01',
  clip_start: '2026-08-30T07:12:01Z', clip_end: '2026-08-30T07:12:16Z',
  clip_seconds: 15.0, frame_count: 131, clip_track_id: 17,
};
const pending = { ...clip, evidence_id: 'EVID-043', media_status: 'PENDING',
  media_url: null, media_bytes: null, source_label: 'GeoVision GEOVISION-D455-01 · CLIP-9ab' };
const seed = {
  evidence_id: 'EVID-001', evidence_type: 'COLLECTION_PROOF',
  captured_at: '2026-08-30T06:00:00Z', verified: true,
  media_status: 'NONE', media_kind: 'none', media_url: null,
  source_ref: '/evidence/EVENT-001_collection_proof.jpg',
  source_label: 'Demo placeholder — no file was ever recorded',
  source_kind: 'PLACEHOLDER', is_placeholder: true,
};

console.log('\n1. the feed offers an evidence action carrying the event id');
const held = M.evidenceAction({ event_id: 'EVENT-006', evidence_count: 2,
  clip_evidence_count: 1, playable_evidence_count: 1 });
t('a held clip renders a clickable button, not a span',
  held.startsWith('<button') && held.includes('class="chip good ev-open"'), held);
t('the button carries the event id the modal will be opened with',
  held.includes('data-ev="EVENT-006"'), held);
t('the label says a clip is playable', held.includes('▶ 1 clip'), held);

const waiting = M.evidenceAction({ event_id: 'EVENT-007', evidence_count: 1,
  clip_evidence_count: 1, playable_evidence_count: 0 });
t('an announced but unfetched clip is offered as pending, not as playable',
  waiting.includes('ev-open') && waiting.includes('1 clip pending') && !waiting.includes('▶'), waiting);

const onlySeed = M.evidenceAction({ event_id: 'EVENT-008', evidence_count: 2,
  clip_evidence_count: 0, playable_evidence_count: 0 });
t('rows with no clip still open the modal, but promise no footage',
  onlySeed.includes('ev-open') && onlySeed.includes('2 evidence') && !onlySeed.includes('▶'), onlySeed);
t('an event with no evidence at all offers no action',
  M.evidenceAction({ event_id: 'E', evidence_count: 0 }) === '');
t('the event id is HTML-escaped into the action',
  M.evidenceAction({ event_id: '"><img src=x>', evidence_count: 1 }).includes('&quot;&gt;&lt;img'));

console.log('\n2. an AVAILABLE clip becomes an HTML5 player');
const card = M.evidenceCard(clip);
t('renders <video> with controls', card.includes('<video') && card.includes('controls'));
t('the source is the WASTRAQ media endpoint, never the edge',
  card.includes('src="/evidence/EVID-042/media"') && !card.includes('http://'), card);
t('preload is metadata, so opening an event does not pull every clip',
  card.includes('preload="metadata"'));
t('a loading state is present before the browser has frames', card.includes('ev-load'));
t('an error state exists and starts hidden',
  card.includes('ev-fail') && /ev-fail[^>]*display:\s*none/.test(card), card);
t('the player is wrapped in a stage keyed by evidence id',
  card.includes('class="ev-stage" data-evid="EVID-042"'));
t('no inline onerror / onload handler on the media element',
  !/on(error|load|canplay)=/.test(card), card);
t('AVAILABLE offers no retrieve button', !card.includes('ev-fetch'));

console.log('\n3. metadata the operator can use, and no path anywhere');
t('clip start and end are shown', card.includes('clip 07:12:01 → 07:12:16'), card);
t('duration and frame count are shown',
  card.includes('15 s') && card.includes('131 frames'), card);
t('the track is shown', card.includes('track 17'));
t('provenance is identity, not location',
  card.includes('origin: GeoVision GEOVISION-D455-01 · CLIP-3f2a1b0c9d8e'), card);
t('the Windows path is not rendered, in any form',
  !card.includes('C:') && !card.includes('\\') && !card.toLowerCase().includes('geovision\\clips'), card);
t('bytes held on this Mac are reported', card.includes('KB held on this Mac'));

const pcard = M.evidenceCard(pending);
t('PENDING renders no player', !pcard.includes('<video') && !pcard.includes('ev-stage'));
t('PENDING keeps the reason and the retrieve button',
  pcard.includes('has not been copied to this Mac yet') && pcard.includes('ev-fetch'));
t('PENDING shows no path either', !pcard.includes('C:') && !pcard.includes('\\'));

console.log('\n4. no dummy evidence is presented as evidence');
t('a placeholder is summarised, not listed',
  M.placeholderNote([seed, clip]).includes('1 placeholder record'));
t('the placeholder note says nothing was recorded',
  M.placeholderNote([seed]).includes('nothing was ever recorded'));
t('no note when every record is real', M.placeholderNote([clip]) === '');
t('a placeholder never yields a player', M.evidencePlayer(seed) === '');
t('a placeholder card shows the placeholder label, not its fake path',
  M.evidenceCard(seed).includes('Demo placeholder') &&
  !M.evidenceCard(seed).includes('EVENT-001_collection_proof.jpg'), M.evidenceCard(seed));

console.log('\n5. provenance fallbacks never reach for a path');
t('missing label falls back to the clip identity',
  M.sourceLabel({ clip_source_id: 'GV-1', clip_id: 'CLIP-9', source_ref: WIN, media_status: 'PENDING' })
    === 'GeoVision GV-1 · CLIP-9');
t('missing label and missing ids fall back to the device, not the file',
  M.sourceLabel({ source_ref: WIN, media_status: 'PENDING' }) === 'GeoVision edge');
t('a record with nothing at all says so', M.sourceLabel({ source_ref: WIN }) === 'No source recorded');
t('a hostile label is escaped',
  M.evidenceCard({ ...seed, source_label: '<script>alert(1)</script>' }).includes('&lt;script&gt;'));

console.log('\n6. the error state is wired, useful, and actionable');
t('a decode failure is explained in operator words',
  M.mediaErrorText(3).includes('could not be decoded'));
t('a missing file is explained as removed from the store',
  M.mediaErrorText(4).includes('removed from the evidence store'));
t('an unknown code still produces a sentence', M.mediaErrorText(99).length > 10);

// wire the real listeners against the small DOM and make the video fail
const stage = el('div', { 'data-evid': 'EVID-042' });
const video = el('video'); video.className = 'ev-media';
const load = el('div'); load.className = 'ev-load';
const failbox = el('div'); failbox.className = 'ev-fail';
stage.children.push(load, video, failbox);
document.stages = [stage];
M.wireEvidenceMedia('EVENT-006');
t('listeners were attached for loading and failure',
  !!video.listeners.loadeddata && !!video.listeners.error);

video.dispatchEvent(new Event('loadeddata'));
t('the loading state clears once the browser has frames', load.style.display === 'none');

video.error = { code: 4 };
video.dispatchEvent(new Event('error'));
t('a failed clip hides the player', video.style.display === 'none');
t('a failed clip shows the reason', failbox.textContent.includes('removed from the evidence store'));
t('a failed clip offers a re-fetch the operator can click',
  failbox.children.some(c => c.tag === 'button' && c.textContent.includes('Re-fetch')));

console.log('\n7. the rest of the dashboard is where it was');
// STEP 4C was a wiring step, not a redesign. These read the raw file rather
// than the extracted helpers, because what they check is that things were
// NOT rewritten: a passing render test says the new code works, only a
// structural check says the old code is still there.
t('the existing evidence modal is reused, not replaced',
  src.includes('id="evm"') && src.includes('id="evm-body"') &&
  (src.match(/class="modal-scrim"/g) || []).length === 1, 'modal markup changed');
t('EVM is still the modal handle bound to that element',
  src.includes("const EVM = WQ.modal('evm')"));
t('the drawer\u2019s View evidence button still opens the same function',
  src.includes("WQ.$('#pd-ev').addEventListener('click', () => { if (drawerEvent) openEvidence(drawerEvent); })"));
t('auto refresh is still constructed and started',
  src.includes('const auto = WQ.autoRefresh(refresh, 10)') && src.includes('auto.start()'));
t('the refresh interval buttons still drive it',
  src.includes("auto.setInterval(Number(b.getAttribute('data-s')))"));
t('pause and refresh-now are still wired',
  src.includes('auto.toggle()') && src.includes('auto.now()'));
t('refresh still repaints the feed, map, KPIs and properties',
  ['renderKpis(', 'renderAnalytics(', 'renderFeed(', 'renderProperties(']
    .every(fn => src.includes(fn)));
t('opening evidence does not touch the polling loop',
  !/openEvidence[\s\S]{0,1200}auto\.(stop|pause)\(/.test(src));
t('the feed row still opens the property drawer',
  src.includes("openProperty(el.getAttribute('data-prop'), el.getAttribute('data-ev'))"));
t('the evidence action stops the row click from firing too',
  /ev-open[\s\S]{0,240}stopPropagation\(\)[\s\S]{0,160}openEvidence\(/.test(src));
t('no second dashboard page was introduced',
  fs.readdirSync(path.join(HERE, '..', 'backend', 'app', 'static'))
    .filter(f => f.endsWith('.html')).length === 3);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
