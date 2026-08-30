import { useEffect, useRef, useState } from "react";
import { API_BASE, postJSON } from "../api";
import {
    ESP32_BASE_URL,
    ESP32_POLL_INTERVAL_MS,
    readLatestUid,
    startScan,
    stopScan,
} from "../esp32";

// This component is the *runtime* bridge: it polls the board and forwards each
// UID to GeoVision's POST /rfid/events, which performs RFID -> track
// attribution and emits RFID_TAP / WORKER_TRACK_BOUND to WASTRAQ. It never
// assigns a card to anyone -- enrolment is the explicit "Assign RFID" modal on
// the collector page, and the two must not be confused.
//
// The reader's address and the three GETs now live in ../esp32 so both screens
// share one configurable URL.
export { ESP32_BASE_URL };

const POLL_INTERVAL_MS = ESP32_POLL_INTERVAL_MS;

function StatusLine({ label, value }) {
    if (value === null || value === undefined || value === "") {
        return null;
    }
    return (
        <div className="status-item">
            {label}: {String(value)}
        </div>
    );
}

function RFIDScanner() {
    const [scanning, setScanning] = useState(false);
    const [message, setMessage] = useState("Idle.");
    const [error, setError] = useState("");
    const [lastUid, setLastUid] = useState("");
    const [result, setResult] = useState(null);
    const [manualUid, setManualUid] = useState("");

    const pollingRef = useRef(null);
    const busyRef = useRef(false);

    const stopPolling = () => {
        if (pollingRef.current) {
            clearInterval(pollingRef.current);
            pollingRef.current = null;
        }
    };

    useEffect(() => stopPolling, []);

    // ------------------------------------------------------------------
    // A UID -- from the reader or typed in -- reaches the backend the same
    // way. /rfid/events always answers 200: an unresolved tap is data, not a
    // transport failure, so every outcome is rendered rather than thrown.
    // ------------------------------------------------------------------
    const submitUid = async (uid) => {
        const clean = String(uid || "").trim().toUpperCase();
        if (!clean) {
            return;
        }

        setLastUid(clean);
        setError("");
        setMessage("Sending " + clean + " to " + API_BASE + "/rfid/events ...");

        try {
            const data = await postJSON("/rfid/events", {
                rfid_uid: clean,
                timestamp: new Date().toISOString(),
            });
            setResult(data);
            setMessage(
                data.status === "BOUND"
                    ? "Bound " + data.collector_id + " to track " + data.track_id + "."
                    : "Tap recorded as " + data.status + "."
            );
        } catch (err) {
            setResult(null);
            setError(err.message || "Backend rejected the tap.");
            setMessage("Tap not recorded.");
        }
    };

    // ------------------------------------------------------------------
    // ESP32 polling
    // ------------------------------------------------------------------
    const readLatest = async () => {
        if (busyRef.current) {
            return;
        }

        try {
            const uid = await readLatestUid();
            if (!uid) {
                return;
            }

            busyRef.current = true;
            await submitUid(uid);

            // Re-arm the reader for the next card.
            try {
                await startScan();
            } catch (err) {
                console.warn("Could not restart ESP32 scan:", err);
            }
            busyRef.current = false;
        } catch (err) {
            console.error("[ESP32] polling error:", err);
            setError("Reader unreachable at " + ESP32_BASE_URL + " -- " + err.message);
        }
    };

    const startScanning = async () => {
        setError("");
        setResult(null);
        setMessage("Connecting to RFID reader...");
        busyRef.current = false;

        try {
            await startScan();
        } catch (err) {
            setError(
                "Could not reach the reader at " + ESP32_BASE_URL +
                    ". Check VITE_ESP32_BASE_URL in frontend/.env.local. " +
                    (err.message || "")
            );
            setMessage("Reader not connected.");
            return;
        }

        setScanning(true);
        setMessage("Place an RFID card on the RC522 reader.");
        stopPolling();
        pollingRef.current = setInterval(readLatest, POLL_INTERVAL_MS);
    };

    const stopScanning = async () => {
        stopPolling();
        setScanning(false);
        setMessage("Stopped.");
        await stopScan();
    };

    const submitManual = async (event) => {
        event.preventDefault();
        await submitUid(manualUid);
    };

    const candidates = result ? result.candidate_track_ids : null;

    return (
        <div className="panel">
            <h2 className="section-title">RFID SCANNER</h2>

            <div className="status-box">
                <StatusLine label="Reader" value={ESP32_BASE_URL} />
                <StatusLine label="Backend" value={API_BASE} />
                <StatusLine label="Polling" value={scanning ? "on" : "off"} />
            </div>

            <div style={{ margin: "12px 0", display: "flex", gap: "8px" }}>
                <button onClick={startScanning} disabled={scanning}>
                    Start scan
                </button>
                <button onClick={stopScanning} disabled={!scanning}>
                    Stop scan
                </button>
            </div>

            <p>{message}</p>
            {error ? <p style={{ color: "#c0392b" }}>{error}</p> : null}

            {/* Diagnostic path: exercises the exact backend call with no
                hardware attached -- what a /rfid-test button was for. */}
            <form onSubmit={submitManual} style={{ margin: "12px 0" }}>
                <label htmlFor="manual-uid">Simulate a tap (no reader): </label>
                <input
                    id="manual-uid"
                    value={manualUid}
                    onChange={(event) => setManualUid(event.target.value)}
                    placeholder="RFID-00455"
                />
                <button type="submit">Send</button>
            </form>

            {result ? (
                <div className="status-box">
                    <StatusLine label="UID" value={lastUid} />
                    <StatusLine label="Status" value={result.status} />
                    <StatusLine label="Resolved" value={String(result.resolved)} />
                    <StatusLine label="Collector" value={result.collector_id} />
                    <StatusLine label="Track" value={result.track_id} />
                    <StatusLine label="Confidence" value={result.confidence} />
                    <StatusLine label="Event id" value={result.event_id} />
                    <StatusLine label="Reason" value={result.reason} />
                    {candidates && candidates.length ? (
                        <StatusLine
                            label="Candidate tracks"
                            value={candidates.join(", ")}
                        />
                    ) : null}
                    {result.evidence ? (
                        <StatusLine
                            label="Evidence clip"
                            value={result.evidence.clip_id + " (" + result.evidence.status + ")"}
                        />
                    ) : null}
                </div>
            ) : null}
        </div>
    );
}

export default RFIDScanner;
