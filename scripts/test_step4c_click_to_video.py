#!/usr/bin/env python3
"""STEP 4C over the wire - the exact path a click takes to a playing clip.

    scripts/run_backend.sh                                 # another terminal
    python3 scripts/test_step4c_click_to_video.py
    python3 scripts/test_step4c_click_to_video.py http://127.0.0.1:8000

stdlib only. READ-ONLY: it creates nothing, changes nothing and deletes
nothing, so it is safe to run against a live demo database mid-demo.

It walks the same four requests the dashboard makes, in the same order, and
checks each one carries what the next one needs:

    GET /collection-events/feed/detailed   the count on the chip
    GET /collection-events/{event_id}      the modal's metadata + evidence
    GET {media_url}                        the bytes, as video/*
    GET {media_url}  (Range: bytes=0-99)   206, so the operator can seek

If nothing on the route has a GeoVision clip yet, that is reported as a
SKIP with the reason - not as a pass, and not as a failure of this step.
Run scripts/test_step4a_evidence_http.py first: it stands up a fake edge and
produces a real clip end to end.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
PASS, FAIL, SKIP = [], [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" +
          (f"\n         {detail}" if detail and not condition else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name}\n         {why}")


def get(path, headers=None):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_json(path):
    status, _, body = get(path)
    try:
        return status, json.loads(body or b"null")
    except json.JSONDecodeError:
        return status, {"raw": body[:200].decode("utf-8", "replace")}


WINDOWSY = ("C:\\", "c:\\", "\\GeoVision", "\\evidence_clips")


def has_windows_path(text) -> bool:
    t = str(text)
    return any(marker in t for marker in WINDOWSY)


def main() -> int:
    print(f"\nWASTRAQ STEP 4C - click to video, against {BASE}\n")

    status, root = get_json("/")
    if status != 200:
        print(f"!! backend not answering on {BASE} (status {status}). "
              f"Start it with scripts/run_backend.sh")
        return 2

    print("1. the feed carries a count the chip can stand behind")
    status, feed = get_json("/collection-events/feed/detailed?limit=200")
    check("the feed responds", status == 200, str(status))
    if status != 200 or not isinstance(feed, list):
        return 1
    check("the feed is not empty", bool(feed),
          "run simulation/simulate_picker.py, or the 4A http test")
    fields = ("evidence_count", "clip_evidence_count",
              "playable_evidence_count", "local_evidence_count")
    if feed:
        row = feed[0]
        for f in fields:
            check(f"every feed row carries {f}", f in row, str(sorted(row)))
        check("no feed row leaks a Windows path",
              not has_windows_path(json.dumps(feed, default=str)))

    flagged = [r for r in feed if r.get("segregation_status") == "NOT_SEGREGATED"]
    check("there is a NOT_SEGREGATED event to click",
          bool(flagged), "the whole step is about that event")

    withclip = [r for r in feed if int(r.get("clip_evidence_count") or 0) > 0]
    playable = [r for r in feed if int(r.get("playable_evidence_count") or 0) > 0]
    print(f"\n     {len(feed)} events · {len(flagged)} not segregated · "
          f"{len(withclip)} with a GeoVision clip · {len(playable)} playable now")

    print("\n2. the modal's single request carries everything it renders")
    target = (playable + withclip + flagged + feed)
    if not target:
        skip("open one event", "no events on this route")
        return 1
    ev_id = target[0]["event_id"]
    status, ev = get_json("/collection-events/" + urllib.parse.quote(ev_id))
    check(f"GET /collection-events/{ev_id} responds", status == 200, str(status))
    if status != 200:
        return 1
    check("the event names the property", bool(ev.get("property_id")))
    check("the event names the collector, not only their id",
          "picker_name" in ev, str(sorted(ev)))
    check("the event carries its own time", bool(ev.get("collection_time")))
    check("the evidence rows come with the event - one request, not two",
          isinstance(ev.get("evidence"), list), str(type(ev.get("evidence"))))
    for item in ev.get("evidence") or []:
        check(f"{item['evidence_id']} carries an operator-safe source_label",
              bool(item.get("source_label")) and not has_windows_path(item["source_label"]),
              str(item.get("source_label")))
        check(f"{item['evidence_id']} declares whether it is a placeholder",
              "is_placeholder" in item)

    print("\n3. a playable clip really plays")
    if not playable:
        skip("fetch the clip bytes",
             "no event on this route has a clip whose bytes are on this Mac. "
             "Run: python3 scripts/test_step4a_evidence_http.py")
    else:
        pid = playable[0]["event_id"]
        _, pev = get_json("/collection-events/" + urllib.parse.quote(pid))
        avail = [i for i in (pev.get("evidence") or [])
                 if i.get("media_status") == "AVAILABLE" and i.get("media_url")]
        check("the event exposes at least one AVAILABLE media_url", bool(avail),
              json.dumps([i.get("media_status") for i in pev.get("evidence") or []]))
        if avail:
            item = avail[0]
            url = item["media_url"]
            check("media_url is a WASTRAQ path, never the edge's",
                  url.startswith("/evidence/") and url.endswith("/media"), url)
            st, hdrs, body = get(url)
            # HTTP header names are case-insensitive and ASGI servers put them
            # on the wire lowercased, so look the name up without case.
            ctype = next((v for k, v in hdrs.items()
                          if k.lower() == "content-type"), "")
            check("the media endpoint returns 200", st == 200, str(st))
            check("it is served as video/* or image/*",
                  ctype.startswith(("video/", "image/")), ctype)
            check("real bytes come back", len(body) > 0, str(len(body)))
            if item.get("media_bytes"):
                check("the length matches what the API reported",
                      len(body) == item["media_bytes"],
                      f"{len(body)} vs {item['media_bytes']}")
            st2, h2, b2 = get(url, {"Range": "bytes=0-99"})
            check("a Range request is answered 206, so the clip can be seeked",
                  st2 == 206, str(st2))
            check("the partial response is the requested length",
                  st2 != 206 or len(b2) == 100, str(len(b2)))
            check("no response header leaks a Windows path",
                  not has_windows_path(json.dumps(dict(hdrs))))
            check("clip timing reached the API",
                  ("clip_start" in item and "clip_seconds" in item), str(sorted(item)))

    print("\n4. missing and unreachable evidence say something useful")
    st, _, body = get("/evidence/EVID-does-not-exist/media")
    check("an unknown evidence id is 404, not a 500", st == 404, str(st))
    check("the 404 says which id it could not find",
          b"EVID-does-not-exist" in body, body[:160].decode("utf-8", "replace"))

    _, allev = get_json("/evidence?limit=200")
    unheld = [e for e in (allev if isinstance(allev, list) else [])
              if e.get("media_status") in ("PENDING", "UNAVAILABLE")]
    if not unheld:
        skip("a clip this Mac does not hold answers 409",
             "every evidence row here is either held or a placeholder - "
             "nothing is in the unheld state to check")
    else:
        eid = unheld[0]["evidence_id"]
        st, _, body = get(f"/evidence/{urllib.parse.quote(eid)}/media")
        text = body.decode("utf-8", "replace")
        check("an announced-but-unheld clip answers 409, never 200 with a path",
              st == 409, str(st))
        check("the 409 explains the state", "media_status" in text, text[:200])
        check("the 409 carries identity, not a Windows path",
              "source_label" in text and not has_windows_path(text), text[:300])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    if SKIP:
        print("\nSkips are states this database is not in, not defects. The 4A "
              "http test creates a real clip end to end:\n"
              "    python3 scripts/test_step4a_evidence_http.py")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
