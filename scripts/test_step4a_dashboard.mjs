// STEP 4A - the evidence modal's render rules, without a browser.
//
//     node scripts/test_step4a_dashboard.mjs
//
// Pulls the media helpers straight out of backend/app/static/dashboard.html
// and runs them against stub data. The one property being checked is the
// one the whole step turns on: a player appears if and only if the backend
// said the bytes are here, and the Windows path is rendered as TEXT - never
// as an href or a src - whatever the status.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(HERE, '..', 'backend', 'app', 'static', 'dashboard.html');

const src = fs.readFileSync(HTML, 'utf8');
const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const start = scripts.indexOf('const MEDIA_NOTE');
const end = scripts.indexOf('async function openEvidence');
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
  timeOf: () => '07:12',
};
const document = { querySelectorAll: () => [] };

const M = new Function('WQ', 'document', helpers +
  'return { evidenceCard, evidencePlayer, mediaFootnote, wireEvidenceFetch };')(WQ, document);

let pass = 0, fail = 0;
const t = (name, cond) => { cond ? pass++ : fail++; console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}`); };

const WIN = 'C:\\GeoVision\\clips\\CLIP-77.mp4';
const available = {
  evidence_id: 'EVID-042', evidence_type: 'VIDEO_CLIP', captured_at: 'x', verified: false,
  media_status: 'AVAILABLE', media_kind: 'video', media_url: '/evidence/EVID-042/media',
  media_bytes: 7685, source_ref: WIN,
};
const pending = { ...available, evidence_id: 'EVID-043', media_status: 'PENDING', media_url: null, media_bytes: null };
const unavailable = { ...pending, evidence_id: 'EVID-044', media_status: 'UNAVAILABLE', fetch_error: 'URLError: connection refused' };
const none = {
  evidence_id: 'EVID-045', evidence_type: 'CAMERA_FRAME', captured_at: 'x', verified: true,
  media_status: 'NONE', media_kind: 'none', media_url: null, source_ref: null,
};

const a = M.evidenceCard(available);
t('AVAILABLE renders a <video> pointing at the WASTRAQ media url',
  a.includes('<video') && a.includes('src="/evidence/EVID-042/media"'));
t('AVAILABLE offers no retrieve button', !a.includes('ev-fetch'));
// STEP 4C tightened this: 4A rendered the Windows path as text, 4C stopped
// rendering it at all. The href/src half of the assertion is unchanged; the
// text half is now the stronger claim.
t('the Windows path never reaches the card, as text or as an href or src',
  a.includes('origin:') && !a.includes('GeoVision\\clips') && !a.includes('C:'));

const p = M.evidenceCard(pending);
t('PENDING renders no player at all', !p.includes('<video'));
t('PENDING offers the retrieve button', p.includes('ev-fetch') && p.includes('data-evid="EVID-043"'));
t('PENDING says why there is nothing to play', p.includes('has not been copied to this Mac yet'));

const u = M.evidenceCard(unavailable);
t('UNAVAILABLE shows the fetch error', u.includes('connection refused'));
t('UNAVAILABLE still offers a retry', u.includes('ev-fetch'));

const n = M.evidenceCard(none);
t('NONE renders neither a player nor a button', !n.includes('<video') && !n.includes('ev-fetch'));

const hostile = M.evidenceCard({
  ...pending, evidence_id: '<img src=x onerror=alert(1)>',
  source_ref: '<script>alert(2)</script>',
});
t('identifiers and source_ref are HTML-escaped',
  !hostile.includes('<img src=x') && !hostile.includes('<script>alert(2)'));

t('the footnote counts what is not held', M.mediaFootnote([available, pending]).includes('1 of 2'));
t('the footnote is silent when everything is held', M.mediaFootnote([available]) === '');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
