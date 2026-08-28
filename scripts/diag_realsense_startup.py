#!/usr/bin/env python3
"""
WASTRAQ hardware diagnostic - explicit stream configuration vs default
pipeline startup on the Intel RealSense D455.

WHAT THIS IS FOR
----------------
`rs-capture` streams. `scripts/test_realsense_picker_tracking.py` fails at
Stage 2 while resolving an explicit `depth 640x480 Z16 @ 15` that the device
says it publishes. The two differ in exactly four ways, and this script takes
them apart one at a time:

    1. explicit `enable_stream(w, h, fmt, fps)`  vs  no config at all
    2. `config.enable_device(serial)`            vs  not pinning the device
    3. `config.can_resolve(pipeline_wrapper)`    vs  going straight to start()
    4. a prior `get_stream_profiles()` sweep     vs  a virgin context

SCOPE - READ THIS
-----------------
This file is standalone and additive. It imports **pyrealsense2 and the Python
standard library and nothing else**. It does not import, read or write any
WASTRAQ module: no vision pipeline, no YOLO, no ByteTrack, no GIS, no
PostgreSQL, no config. It changes no existing file. Deleting it leaves the
project byte-identical.

WHY SUBPROCESSES
----------------
Every variant runs in its **own** freshly-forked process. A contaminated
librealsense context is one of the things under test, and it cannot be tested
from inside a process that is already contaminated. One process per variant is
the only way each row means what it says.

THE IMPORTANT DESIGN CHOICE
---------------------------
`can_resolve()` returning False is recorded but **never treated as fatal**.
The harness attempts `pipeline.start()` afterwards regardless. If a variant
reports `resolve=NO  start=OK  frames=45`, then `can_resolve` is a broken gate
and the profile was always fine - which is a completely different bug from the
device genuinely refusing the profile.

USAGE
-----
    sudo .venv/bin/python scripts/diag_realsense_startup.py
    sudo .venv/bin/python scripts/diag_realsense_startup.py --only D,G,H
    sudo .venv/bin/python scripts/diag_realsense_startup.py --seconds 5
    sudo .venv/bin/python scripts/diag_realsense_startup.py --list

Run it with nothing else holding the camera: no backend, no realsense-viewer,
no rs-capture.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

RESULT_MARKER = "##RESULT##"

BOLD = "\033[1m" if sys.stdout.isatty() else ""
DIM = "\033[2m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
GREEN = "\033[32m" if sys.stdout.isatty() else ""
YELLOW = "\033[33m" if sys.stdout.isatty() else ""
RESET = "\033[0m" if sys.stdout.isatty() else ""


# --------------------------------------------------------------- matrix --
#
# streams:
#   None -> no rs.config() object at all; pipeline.start() with no argument.
#           This is what rs-capture does.
#   []   -> an empty rs.config() is created and passed, but nothing enabled.
#   [{...}] -> one enable_stream call per dict. A dict with only "stream"
#           enables the stream type and lets the device pick everything else.
#
# enable_device / can_resolve / enumerate_first mirror the three things
# RealSenseSource.open() added on 2026-08-22 and the old code never did.

D = "depth"
C = "color"


def _d(w, h, fps, fmt="z16"):
    return {"stream": D, "w": w, "h": h, "fmt": fmt, "fps": fps}


VARIANTS: dict[str, dict] = {
    # ---- baselines: what rs-capture actually does -----------------------
    "A": {
        "label": "no config at all - pipeline.start() (rs-capture equivalent)",
        "streams": None, "enable_device": False, "can_resolve": False,
        "enumerate_first": False,
    },
    "B": {
        "label": "empty rs.config(), nothing enabled",
        "streams": [], "enable_device": False, "can_resolve": False,
        "enumerate_first": False,
    },
    "C": {
        "label": "enable_stream(depth) - stream type only, device picks the rest",
        "streams": [{"stream": D}], "enable_device": False, "can_resolve": False,
        "enumerate_first": False,
    },

    # ---- the requested profile, adding one suspect at a time ------------
    "D": {
        "label": "explicit depth 640x480 Z16 @15 - nothing else",
        "streams": [_d(640, 480, 15)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },
    "E": {
        "label": "explicit + enable_device(serial)",
        "streams": [_d(640, 480, 15)], "enable_device": True,
        "can_resolve": False, "enumerate_first": False,
    },
    "F": {
        "label": "explicit + can_resolve() probe (start attempted regardless)",
        "streams": [_d(640, 480, 15)], "enable_device": False,
        "can_resolve": True, "enumerate_first": False,
    },
    "G": {
        "label": "explicit + enable_device + can_resolve  <- current open() path",
        "streams": [_d(640, 480, 15)], "enable_device": True,
        "can_resolve": True, "enumerate_first": False,
    },
    "H": {
        "label": "G, but a prior sensor sweep is held open  <- Stage1+Stage2 path",
        "streams": [_d(640, 480, 15)], "enable_device": True,
        "can_resolve": True, "enumerate_first": True,
    },

    # ---- is it this particular profile? ---------------------------------
    "I": {
        "label": "explicit depth 848x480 Z16 @15 (D455 native depth width)",
        "streams": [_d(848, 480, 15)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },
    "J": {
        "label": "explicit depth 640x480 Z16 @30",
        "streams": [_d(640, 480, 30)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },
    "K": {
        "label": "explicit depth 848x480 Z16 @30",
        "streams": [_d(848, 480, 30)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },
    "L": {
        "label": "explicit depth 480x270 Z16 @15 (smallest, USB2-friendly)",
        "streams": [_d(480, 270, 15)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },
    "M": {
        "label": "explicit depth 640x480 Z16 @6 (lowest rate)",
        "streams": [_d(640, 480, 6)], "enable_device": False,
        "can_resolve": False, "enumerate_first": False,
    },

    # ---- bandwidth: does adding colour break a working depth stream? ----
    "N": {
        "label": "explicit depth + color, both 640x480 @15 (no alignment)",
        "streams": [_d(640, 480, 15),
                    {"stream": C, "w": 640, "h": 480, "fmt": "bgr8", "fps": 15}],
        "enable_device": False, "can_resolve": False, "enumerate_first": False,
    },
}

ORDER = list(VARIANTS.keys())


# ----------------------------------------------------------------- child --

def _fmt_of(rs, name: str):
    return getattr(rs.format, name)


def _stream_of(rs, name: str):
    return getattr(rs.stream, name)


def _profile_of_frame(frame) -> dict | None:
    """What the device ACTUALLY delivered, read off the frame itself."""
    try:
        vsp = frame.profile.as_video_stream_profile()
        return {
            "stream": str(frame.profile.stream_type()).rsplit(".", 1)[-1].lower(),
            "w": int(vsp.width()), "h": int(vsp.height()),
            "fmt": str(frame.profile.format()).rsplit(".", 1)[-1].lower(),
            "fps": int(frame.profile.fps()),
        }
    except Exception:  # noqa: BLE001
        return None


def _describe(p: dict | None) -> str:
    if not p or "w" not in p:
        return "-"
    return f"{p['w']}x{p['h']} {p['fmt'].upper()}@{p['fps']}"


def _req_label(streams) -> str:
    """Short human label for what a variant asked the device for."""
    if streams is None:
        return "(no config)"
    if not streams:
        return "(empty config)"
    parts = []
    for s in streams:
        if "w" in s:
            parts.append(f"{s['stream'][0]}:{s['w']}x{s['h']}@{s['fps']}")
        else:
            parts.append(f"{s['stream']}:type-only")
    return " + ".join(parts)


def run_child(vid: str, seconds: float, startup_ms: int) -> int:
    try:
        import pyrealsense2 as rs  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(RESULT_MARKER + json.dumps({
            "id": "?", "frames": 0, "start": False, "resolve": None,
            "requested": "-", "first_frame_ms": None, "fps": 0.0,
            "delivered_depth": None, "delivered_color": None, "notes": [],
            "error": f"pyrealsense2 is not importable ({exc}). Use the venv "
                     f"python that has the hand-built .so: "
                     f"sudo .venv/bin/python scripts/diag_realsense_startup.py",
            "start_error": f"pyrealsense2 not importable: {exc}",
        }), flush=True)
        return 1

    v = VARIANTS[vid]
    out: dict = {
        "id": vid, "label": v["label"],
        "requested": _req_label(v["streams"]),
        "usb": None, "serial": None,
        "resolve": None, "resolve_error": None,
        "start": False, "start_error": None,
        "first_frame_ms": None, "frames": 0, "fps": 0.0,
        "delivered_depth": None, "delivered_color": None,
        "stopped_cleanly": False, "notes": [],
    }

    def note(msg: str) -> None:
        out["notes"].append(msg)
        print(f"  {DIM}{msg}{RESET}", flush=True)

    held = None  # kept alive on purpose for the enumerate_first variant

    try:
        ctx = rs.context()
        devices = list(ctx.query_devices())
        if not devices:
            out["start_error"] = "no RealSense device found on USB"
            print(RESULT_MARKER + json.dumps(out), flush=True)
            return 1
        dev = devices[0]
        for attr, key in (("serial_number", "serial"),
                          ("usb_type_descriptor", "usb")):
            try:
                out[key] = dev.get_info(getattr(rs.camera_info, attr))
            except Exception:  # noqa: BLE001
                pass

        if v["enumerate_first"]:
            # Reproduce Stage 1: sweep every sensor's profiles and KEEP the
            # device (and its sensor objects) alive across the start below.
            sensors = list(dev.query_sensors())
            n = 0
            for s in sensors:
                try:
                    n += len(list(s.get_stream_profiles()))
                except Exception:  # noqa: BLE001
                    pass
            held = (ctx, dev, sensors)
            note(f"prior sweep: {len(sensors)} sensors, {n} profiles, handles held open")

        pipe = rs.pipeline()
        cfg = None
        if v["streams"] is not None:
            cfg = rs.config()
            if v["enable_device"] and out["serial"]:
                cfg.enable_device(out["serial"])
                note(f"enable_device({out['serial']})")
            for s in v["streams"]:
                if "w" in s:
                    cfg.enable_stream(_stream_of(rs, s["stream"]), s["w"], s["h"],
                                      _fmt_of(rs, s["fmt"]), s["fps"])
                    note(f"enable_stream({s['stream']}, {s['w']}x{s['h']}, "
                         f"{s['fmt']}, {s['fps']})")
                else:
                    cfg.enable_stream(_stream_of(rs, s["stream"]))
                    note(f"enable_stream({s['stream']})  [type only]")

        # ---- can_resolve, recorded but NEVER fatal ---------------------
        if cfg is not None and v["can_resolve"]:
            try:
                out["resolve"] = bool(cfg.can_resolve(rs.pipeline_wrapper(pipe)))
            except Exception as exc:  # noqa: BLE001
                out["resolve"] = False
                out["resolve_error"] = f"{type(exc).__name__}: {exc}"
            note(f"can_resolve -> {out['resolve']}"
                 + (f"  ({out['resolve_error']})" if out["resolve_error"] else ""))
            note("continuing to start() regardless - a False here is a claim to "
                 "be tested, not obeyed")

        # ---- start ------------------------------------------------------
        try:
            t0 = time.time()
            pipe.start(cfg) if cfg is not None else pipe.start()
            out["start"] = True
            note(f"start() returned after {(time.time() - t0) * 1000:.0f} ms")
        except Exception as exc:  # noqa: BLE001
            out["start_error"] = f"{type(exc).__name__}: {exc}"
            print(RESULT_MARKER + json.dumps(out), flush=True)
            return 2

        # ---- first frame + throughput ----------------------------------
        try:
            t0 = time.time()
            fs = pipe.wait_for_frames(startup_ms)
            out["first_frame_ms"] = round((time.time() - t0) * 1000.0, 1)
            out["frames"] = 1
            for f in fs:
                p = _profile_of_frame(f)
                if not p:
                    continue
                if p["stream"] == "depth" and not out["delivered_depth"]:
                    out["delivered_depth"] = p
                elif p["stream"] == "color" and not out["delivered_color"]:
                    out["delivered_color"] = p

            deadline = time.time() + seconds
            t_start = time.time()
            while time.time() < deadline:
                pipe.wait_for_frames(5000)
                out["frames"] += 1
            out["fps"] = round(out["frames"] / max(time.time() - t_start, 1e-6), 1)
        except Exception as exc:  # noqa: BLE001
            out["start_error"] = f"streaming: {type(exc).__name__}: {exc}"

        try:
            pipe.stop()
            out["stopped_cleanly"] = True
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"stop() raised: {exc}")

    except Exception as exc:  # noqa: BLE001
        out["start_error"] = out["start_error"] or f"{type(exc).__name__}: {exc}"
    finally:
        del held

    print(RESULT_MARKER + json.dumps(out), flush=True)
    return 0 if out["frames"] > 0 else 3


def run_inventory() -> int:
    """Everything the device publishes, printed once, from a virgin context."""
    try:
        import pyrealsense2 as rs  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(RESULT_MARKER + json.dumps({
            "id": "?", "frames": 0, "start": False, "resolve": None,
            "requested": "-", "first_frame_ms": None, "fps": 0.0,
            "delivered_depth": None, "delivered_color": None, "notes": [],
            "error": f"pyrealsense2 is not importable ({exc}). Use the venv "
                     f"python that has the hand-built .so: "
                     f"sudo .venv/bin/python scripts/diag_realsense_startup.py",
            "start_error": f"pyrealsense2 not importable: {exc}",
        }), flush=True)
        return 1

    out: dict = {"id": "INV", "device": None, "serial": None, "firmware": None,
                 "usb": None, "depth": [], "color": [], "sensors": []}
    ctx = rs.context()
    devices = list(ctx.query_devices())
    if not devices:
        out["error"] = "no RealSense device found on USB"
        print(RESULT_MARKER + json.dumps(out), flush=True)
        return 1
    dev = devices[0]
    for attr, key in (("name", "device"), ("serial_number", "serial"),
                      ("firmware_version", "firmware"),
                      ("usb_type_descriptor", "usb")):
        try:
            out[key] = dev.get_info(getattr(rs.camera_info, attr))
        except Exception:  # noqa: BLE001
            pass

    seen: dict[str, set] = {"depth": set(), "color": set()}
    for sensor in dev.query_sensors():
        try:
            sname = sensor.get_info(rs.camera_info.name)
        except Exception:  # noqa: BLE001
            sname = "?"
        out["sensors"].append(sname)
        for sp in sensor.get_stream_profiles():
            try:
                if not sp.is_video_stream_profile():
                    continue
                vsp = sp.as_video_stream_profile()
                st = str(sp.stream_type()).rsplit(".", 1)[-1].lower()
                if st not in seen:
                    continue
                seen[st].add((int(vsp.width()), int(vsp.height()),
                              str(sp.format()).rsplit(".", 1)[-1].lower(),
                              int(sp.fps())))
            except Exception:  # noqa: BLE001
                continue
    for st in ("depth", "color"):
        out[st] = sorted(seen[st], key=lambda r: (-r[0] * r[1], r[2], -r[3]))
    print(RESULT_MARKER + json.dumps(out), flush=True)
    return 0


# ---------------------------------------------------------------- parent --

def spawn(args_list: list[str], timeout: float) -> tuple[dict | None, str]:
    proc = subprocess.run(  # noqa: PLW1510
        [sys.executable, os.path.abspath(__file__), *args_list],
        capture_output=True, text=True, timeout=timeout,
    )
    blob = None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                blob = json.loads(line[len(RESULT_MARKER):])
            except Exception:  # noqa: BLE001
                pass
    tail = (proc.stdout + proc.stderr).strip()
    return blob, tail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--child", metavar="ID", help=argparse.SUPPRESS)
    ap.add_argument("--inventory-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--only", default="", help="comma-separated variant ids, e.g. D,G,H")
    ap.add_argument("--seconds", type=float, default=3.0,
                    help="how long to keep pulling frames per variant (default 3)")
    ap.add_argument("--startup-ms", type=int, default=15000,
                    help="first-frame timeout in ms (default 15000)")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="pause between variants, seconds (default 1.5)")
    ap.add_argument("--list", action="store_true", help="print the matrix and exit")
    a = ap.parse_args()

    if a.child:
        return run_child(a.child, a.seconds, a.startup_ms)
    if a.inventory_child:
        return run_inventory()

    if a.list:
        for k in ORDER:
            print(f"  {k}  {VARIANTS[k]['label']}")
        return 0

    ids = [s.strip().upper() for s in a.only.split(",") if s.strip()] or ORDER
    bad = [i for i in ids if i not in VARIANTS]
    if bad:
        print(f"unknown variant(s): {', '.join(bad)}")
        return 2

    print(f"{BOLD}RealSense startup-strategy isolation{RESET}")
    print(f"{DIM}one fresh process per variant; can_resolve is recorded, never obeyed{RESET}")
    if os.geteuid() != 0:
        print(f"{YELLOW}warning: not running as root. On this Mac librealsense "
              f"needs sudo, and every row below will fail identically.{RESET}")
    print()

    # ---- inventory ------------------------------------------------------
    inv, tail = spawn(["--inventory-child"], timeout=60)
    if not inv or inv.get("error"):
        print(f"{RED}inventory failed: {(inv or {}).get('error') or tail}{RESET}")
        return 1
    print(f"{BOLD}device{RESET}    {inv['device']}  serial {inv['serial']}  "
          f"fw {inv['firmware']}")
    usb = str(inv["usb"])
    usb_col = GREEN if usb.startswith("3") else YELLOW
    print(f"{BOLD}USB{RESET}       {usb_col}{usb}{RESET}")
    print(f"{BOLD}sensors{RESET}   {', '.join(inv['sensors'])}")
    print(f"{BOLD}depth{RESET}     {len(inv['depth'])} published profiles")
    for w, h, f, fps in inv["depth"][:40]:
        star = ""
        if (w, h, f) == (640, 480, "z16"):
            star = f"  {GREEN}<- the contested one{RESET}"
        print(f"          {w}x{h} {f.upper()} @ {fps}{star}")
    if len(inv["depth"]) > 40:
        print(f"          {DIM}... {len(inv['depth']) - 40} more{RESET}")
    print(f"{BOLD}color{RESET}     {len(inv['color'])} published profiles")
    print()

    # ---- variants -------------------------------------------------------
    rows: list[dict] = []
    for i, vid in enumerate(ids):
        print(f"{BOLD}[{vid}]{RESET} {VARIANTS[vid]['label']}")
        try:
            blob, tail = spawn(
                ["--child", vid, "--seconds", str(a.seconds),
                 "--startup-ms", str(a.startup_ms)],
                timeout=a.seconds + (a.startup_ms / 1000.0) + 45,
            )
        except subprocess.TimeoutExpired:
            blob, tail = None, "child process timed out and was killed"
        if blob is None:
            blob = {"id": vid, "label": VARIANTS[vid]["label"], "frames": 0,
                    "start": False, "start_error": f"child produced no result: {tail[-300:]}",
                    "resolve": None, "requested": "?", "first_frame_ms": None,
                    "fps": 0.0, "delivered_depth": None, "delivered_color": None,
                    "stopped_cleanly": False, "notes": []}
        for n in blob.get("notes", []):
            print(f"  {DIM}{n}{RESET}")
        ok = blob.get("frames", 0) > 0
        if ok:
            print(f"  {GREEN}PASS{RESET}  first frame {blob['first_frame_ms']} ms, "
                  f"{blob['frames']} frames, {blob['fps']} fps, "
                  f"delivered {_describe(blob.get('delivered_depth'))}")
        else:
            print(f"  {RED}FAIL{RESET}  {blob.get('start_error') or 'no frames'}")
        rows.append(blob)
        print()
        if i + 1 < len(ids):
            time.sleep(a.settle)

    # ---- matrix ---------------------------------------------------------
    print(f"{BOLD}SUMMARY{RESET}")
    hdr = (f"{'id':<3} {'requested':<28} {'resolve':<8} {'start':<6} "
           f"{'frames':<7} {'delivered depth':<20} note")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        res = {True: "yes", False: "NO", None: "-"}[r.get("resolve")]
        st = "ok" if r.get("start") else "NO"
        note = "" if r.get("frames", 0) else (r.get("start_error") or "no frames")
        print(f"{r['id']:<3} {str(r.get('requested')):<28} {res:<8} {st:<6} "
              f"{r.get('frames', 0):<7} {_describe(r.get('delivered_depth')):<20} "
              f"{note[:60]}")
    print()

    # ---- what the matrix means ------------------------------------------
    by = {r["id"]: r for r in rows}

    def ran(*ks: str) -> bool:
        return all(k in by for k in ks)

    def passed(k: str) -> bool:
        return bool(by.get(k, {}).get("frames", 0))

    print(f"{BOLD}READING{RESET}")
    said = 0
    for k in ("F", "G", "H"):
        r = by.get(k)
        if r and r.get("resolve") is False and r.get("frames", 0) > 0:
            said += 1
            print(f"  {YELLOW}*{RESET} [{k}] can_resolve said NO and the stream "
                  f"then delivered {r['frames']} frames. The can_resolve gate "
                  f"is wrong, not the profile.")
    if ran("D", "G") and passed("D") and not passed("G"):
        said += 1
        print(f"  {YELLOW}*{RESET} explicit config alone works; adding "
              f"enable_device+can_resolve breaks it. The additions are the fault.")
    if ran("D", "G", "H") and passed("D") and passed("G") and not passed("H"):
        said += 1
        print(f"  {YELLOW}*{RESET} only the variant with a held-open sensor sweep "
              f"fails. Stage 1's retained device handle is the fault.")
    if ran("A", "D") and passed("A") and not passed("D"):
        said += 1
        print(f"  {YELLOW}*{RESET} default start works, explicit 640x480@15 does not. "
              f"The requested profile is genuinely not startable on this link.")
    if ran("D", "I") and passed("I") and not passed("D"):
        said += 1
        print(f"  {YELLOW}*{RESET} 848x480 works where 640x480 does not - 640x480 "
              f"depth is a published-but-not-startable mode on this link.")
    if ran("D", "N") and passed("D") and not passed("N"):
        said += 1
        print(f"  {YELLOW}*{RESET} depth alone works, depth+colour does not: "
              f"bandwidth, i.e. the USB {usb} link.")
    a_row = by.get("A")
    if a_row and passed("A") and a_row.get("delivered_depth"):
        said += 1
        print(f"  {YELLOW}*{RESET} the device's OWN recommended depth mode is "
              f"{_describe(a_row['delivered_depth'])} - that is what rs-capture "
              f"gets, and what an unconfigured pipeline would use.")
    if not said:
        print(f"  {DIM}no single-cause pattern matched; read the matrix above.{RESET}")
    if not any(passed(k) for k in ids):
        print(f"  {RED}*{RESET} nothing started at all. Check sudo, and that no "
              f"backend / rs-capture / realsense-viewer holds the camera.")
    print()
    print(f"{DIM}Nothing was modified. This script imports pyrealsense2 and the "
          f"standard library only.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
