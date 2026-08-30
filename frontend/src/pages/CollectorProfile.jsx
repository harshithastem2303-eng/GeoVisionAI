import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { API_BASE } from "../api";
import {
    ESP32_BASE_URL,
    ESP32_POLL_INTERVAL_MS,
    readLatestUid,
    startScan,
    stopScan,
} from "../esp32";

const collectionHistory = {
    GC001: [
        ["H1024", "10:32 AM", "Segregated"],
        ["H1025", "10:38 AM", "Non-Segregated"],
        ["H1026", "10:44 AM", "Segregated"],
    ],
    GC002: [
        ["H1101", "09:48 AM", "Segregated"],
        ["H1102", "09:54 AM", "Segregated"],
        ["H1103", "10:01 AM", "Segregated"],
    ],
    GC003: [
        ["H1201", "10:15 AM", "Segregated"],
        ["H1202", "10:21 AM", "Non-Segregated"],
        ["H1203", "10:29 AM", "Segregated"],
    ],
    GC004: [],
};

function CollectorProfile() {
    const { id } = useParams();
    const navigate = useNavigate();

    const [collector, setCollector] = useState(null);
    const [rfids, setRfids] = useState([]);
    const [assignedRfid, setAssignedRfid] = useState(null);
    const [showRfidModal, setShowRfidModal] = useState(false);
    const [rfidInput, setRfidInput] = useState("");
    const [selectedRfid, setSelectedRfid] = useState("");
    const [rfidError, setRfidError] = useState("");
    const [loading, setLoading] = useState(true);
    const [savingRfid, setSavingRfid] = useState(false);
    const [pageError, setPageError] = useState("");

    // ------------------------------------------------------------------
    // Card enrolment by scan.
    //
    // This is deliberately separate from the runtime tap path in
    // components/RFIDScanner.jsx. That one asks "who is holding this card and
    // which tracked person are they?"; this one asks "which card should this
    // named collector carry from now on?". A tap outside this modal must never
    // reassign anything, so the poller only exists while the modal is open and
    // scanPhase is "waiting", and it is torn down on every exit path.
    // ------------------------------------------------------------------
    const [scanPhase, setScanPhase] = useState("idle");
    const [scannedUid, setScannedUid] = useState("");

    const scanTimerRef = useRef(null);
    const scanAbortRef = useRef(null);
    const readerArmedRef = useRef(false);
    // Latched the instant a non-empty UID is seen, before any await. The board
    // keeps returning the same UID until it is re-armed, so without this the
    // next poll tick would start a second assignment for the same card.
    const uidClaimedRef = useRef(false);

    const loadCollector = async (options = {}) => {
        const silent = options.silent === true;
        try {
            if (!silent) {
                setLoading(true);
            }
            setPageError("");

            const [collectorResponse, rfidResponse] = await Promise.all([
                fetch(`${API_BASE}/collectors/${id}`),
                fetch(`${API_BASE}/rfids`),
            ]);

            const collectorData = await collectorResponse.json();

            if (!collectorResponse.ok) {
                throw new Error(
                    collectorData.detail || "Collector not found"
                );
            }

            const rfidData = await rfidResponse.json();

            if (!rfidResponse.ok) {
                throw new Error(
                    rfidData.detail || "Could not load RFID cards"
                );
            }

            setCollector(collectorData);
            setAssignedRfid(collectorData.rfid || null);
            setRfids(rfidData);
        } catch (error) {
            console.error("Failed to load collector:", error);
            // A silent refresh runs behind an open modal. Blanking the page
            // with an error there would destroy the success state the user is
            // still reading, so the refresh failure is logged and the modal's
            // own optimistic update stands.
            if (!silent) {
                setPageError(
                    error.message ||
                        "Could not connect to the GeoVision backend."
                );
            }
        } finally {
            if (!silent) {
                setLoading(false);
            }
        }
    };

    useEffect(() => {
        loadCollector();
    }, [id]);

    // ------------------------------------------------------------------
    // Scan lifecycle
    // ------------------------------------------------------------------

    /** Clear the poller and abort any request still in flight. Synchronous. */
    const stopPolling = useCallback(() => {
        if (scanTimerRef.current) {
            clearInterval(scanTimerRef.current);
            scanTimerRef.current = null;
        }
        if (scanAbortRef.current) {
            scanAbortRef.current.abort();
            scanAbortRef.current = null;
        }
    }, []);

    /**
     * Full teardown: stop polling, and disarm the board if we armed it.
     * Safe to call twice, and safe to call when nothing was ever started.
     */
    const teardownScan = useCallback(async () => {
        stopPolling();
        uidClaimedRef.current = false;
        if (readerArmedRef.current) {
            readerArmedRef.current = false;
            await stopScan();
        }
    }, [stopPolling]);

    // Unmounting mid-scan (browser back, route change) must not leave the
    // reader armed or an interval running.
    useEffect(() => {
        return () => {
            stopPolling();
            if (readerArmedRef.current) {
                readerArmedRef.current = false;
                stopScan();
            }
        };
    }, [stopPolling]);

    const closeRfidModal = useCallback(() => {
        teardownScan();
        setShowRfidModal(false);
        setScanPhase("idle");
        setScannedUid("");
        setRfidError("");
        setSelectedRfid("");
        setRfidInput("");
    }, [teardownScan]);

    /**
     * Assign the card the reader just read to the collector this modal is for.
     *
     * ``register_if_unknown=true`` is passed only here: a UID coming off a
     * physical card cannot be expected to already exist as a row, whereas the
     * manual-entry path below deliberately keeps the original 404 so a typo
     * cannot create a card.
     */
    const assignScannedUid = useCallback(
        async (uid) => {
            setScanPhase("assigning");
            setSavingRfid(true);
            setRfidError("");

            try {
                const params = new URLSearchParams({
                    collector_id: collector.id,
                    rfid_id: uid,
                    register_if_unknown: "true",
                });

                const response = await fetch(
                    `${API_BASE}/rfids/assign?${params.toString()}`,
                    { method: "POST" }
                );

                const data = await response.json().catch(() => ({}));

                if (!response.ok) {
                    // 409 from the backend means the card belongs to someone
                    // else. Surfaced as-is -- nothing is overwritten.
                    throw new Error(
                        data.detail || `Failed to assign RFID (${response.status})`
                    );
                }

                setAssignedRfid(data.rfid_id);
                setCollector((current) => ({
                    ...current,
                    rfid: data.rfid_id,
                    waste_state: data.waste_state,
                }));
                setScanPhase("success");

                // Pull the authoritative rows back without flipping the page
                // into its loading state under the open modal.
                await loadCollector({ silent: true });
            } catch (error) {
                console.error("RFID assignment failed:", error);
                setRfidError(error.message || "Failed to assign RFID.");
                setScanPhase("error");
            } finally {
                setSavingRfid(false);
            }
        },
        [collector, id]
    );

    /** One poll tick. Only ever installed while the modal is open. */
    const pollForUid = useCallback(async () => {
        if (uidClaimedRef.current) {
            return;
        }

        let uid = "";
        try {
            const controller = new AbortController();
            scanAbortRef.current = controller;
            uid = await readLatestUid(controller.signal);
        } catch (error) {
            if (error.name === "AbortError") {
                return;
            }
            stopPolling();
            readerArmedRef.current = false;
            stopScan();
            setRfidError(
                `Lost the reader at ${ESP32_BASE_URL} — ${error.message || "unreachable"}`
            );
            setScanPhase("error");
            return;
        }

        if (!uid || uidClaimedRef.current) {
            return;
        }

        // Latch first, then stop everything, then assign. The board repeats
        // the same UID until re-armed, so this ordering is what makes a
        // duplicate response harmless.
        uidClaimedRef.current = true;
        stopPolling();
        readerArmedRef.current = false;
        stopScan();

        setScannedUid(uid);
        setScanPhase("detected");
        await assignScannedUid(uid);
    }, [assignScannedUid, stopPolling]);

    // The poller is an effect, not a loose setInterval, so React owns its
    // lifetime: it exists only while the modal is open AND waiting, and any
    // change to either condition tears it down.
    useEffect(() => {
        if (!showRfidModal || scanPhase !== "waiting") {
            return undefined;
        }
        const timer = setInterval(pollForUid, ESP32_POLL_INTERVAL_MS);
        scanTimerRef.current = timer;
        return () => {
            clearInterval(timer);
            if (scanTimerRef.current === timer) {
                scanTimerRef.current = null;
            }
        };
    }, [showRfidModal, scanPhase, pollForUid]);

    const beginScan = useCallback(async () => {
        setRfidError("");
        setScannedUid("");
        uidClaimedRef.current = false;
        setScanPhase("arming");

        try {
            await startScan();
            readerArmedRef.current = true;
        } catch (error) {
            readerArmedRef.current = false;
            setRfidError(
                `Could not reach the reader at ${ESP32_BASE_URL}. ` +
                    "Check VITE_ESP32_BASE_URL in frontend/.env.local. " +
                    (error.message || "")
            );
            setScanPhase("error");
            return;
        }

        setScanPhase("waiting");
    }, []);

    const cancelScan = useCallback(async () => {
        await teardownScan();
        setScanPhase("idle");
        setScannedUid("");
    }, [teardownScan]);

    /**
     * True whenever the manual half of the modal must not be touched: an
     * assignment is in flight, or the reader is armed and a card could land
     * at any moment.
     */
    const scanBusy =
        savingRfid ||
        scanPhase === "arming" ||
        scanPhase === "waiting" ||
        scanPhase === "detected" ||
        scanPhase === "assigning";

    // Let the success state be read, then get out of the way. "Done" does the
    // same thing immediately.
    useEffect(() => {
        if (!showRfidModal || scanPhase !== "success") {
            return undefined;
        }
        const timer = setTimeout(closeRfidModal, 1800);
        return () => clearTimeout(timer);
    }, [showRfidModal, scanPhase, closeRfidModal]);

    if (loading) {
        return (
            <div
                style={{
                    minHeight: "100vh",
                    background: "#0d1726",
                    color: "#ffffff",
                    padding: "40px",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontFamily:
                        "Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                }}
            >
                Loading collector...
            </div>
        );
    }

    if (!collector) {
        return (
            <div
                style={{
                    minHeight: "100vh",
                    background: "#0d1726",
                    color: "#ffffff",
                    padding: "40px",
                    fontFamily:
                        "Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                }}
            >
                <button
                    onClick={() => navigate("/garbage-collectors")}
                    style={{
                        padding: "10px 15px",
                        borderRadius: "8px",
                        border: "1px solid #475569",
                        background: "#111c2c",
                        color: "#ffffff",
                        cursor: "pointer",
                        fontWeight: 700,
                    }}
                >
                    ← Garbage Collectors
                </button>

                <h1 style={{ marginTop: "30px" }}>
                    {pageError || "Collector not found"}
                </h1>
            </div>
        );
    }

    const history = collectionHistory[collector.id] || [];
    const total = history.length;
    const segregated = history.filter(
        (item) => item[2] === "Segregated"
    ).length;
    const nonSegregated = total - segregated;
    const segregationRate =
        total > 0 ? ((segregated / total) * 100).toFixed(1) : "0.0";

    return (
        <div
            style={{
                minHeight: "100vh",
                background: "#0d1726",
                color: "#f8fafc",
                padding: "20px",
                boxSizing: "border-box",
                fontFamily:
                    "Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
            }}
        >
            <div
                style={{
                    background: "#1d2a3a",
                    border: "1px solid #334155",
                    borderRadius: "14px",
                    padding: "22px 28px",
                    marginBottom: "20px",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "20px",
                    flexWrap: "wrap",
                }}
            >
                <div>
                    <button
                        onClick={() => navigate("/garbage-collectors")}
                        style={{
                            border: "none",
                            background: "transparent",
                            color: "#93c5fd",
                            padding: 0,
                            cursor: "pointer",
                            fontWeight: 700,
                            fontSize: "14px",
                        }}
                    >
                        ← Garbage Collectors
                    </button>

                    <h1
                        style={{
                            margin: "12px 0 5px",
                            fontSize: "32px",
                            fontWeight: 800,
                        }}
                    >
                        {collector.name}
                    </h1>

                    <div style={{ color: "#94a3b8" }}>
                        {collector.id} · {collector.area}
                    </div>
                </div>

                <span
                    style={{
                        padding: "8px 14px",
                        borderRadius: "20px",
                        background:
                            collector.status === "Active"
                                ? "#dcfce7"
                                : "#fee2e2",
                        color:
                            collector.status === "Active"
                                ? "#166534"
                                : "#991b1b",
                        fontWeight: 800,
                    }}
                >
                    ● {collector.status}
                </span>
            </div>

            <div
                style={{
                    display: "grid",
                    gridTemplateColumns:
                        "repeat(auto-fit, minmax(250px, 1fr))",
                    gap: "18px",
                    marginBottom: "20px",
                }}
            >
                <section
                    style={{
                        background: "#ffffff",
                        color: "#111827",
                        borderRadius: "12px",
                        padding: "22px",
                    }}
                >
                    <h2 style={{ marginTop: 0 }}>Contact Information</h2>

                    <div style={{ marginTop: "18px" }}>
                        <div
                            style={{
                                color: "#6b7280",
                                fontSize: "13px",
                                marginBottom: "5px",
                            }}
                        >
                            Phone
                        </div>
                        <strong>{collector.phone}</strong>
                    </div>

                    <div style={{ marginTop: "18px" }}>
                        <div
                            style={{
                                color: "#6b7280",
                                fontSize: "13px",
                                marginBottom: "5px",
                            }}
                        >
                            Assigned Area
                        </div>
                        <strong>{collector.area}</strong>
                    </div>
                </section>

                <section
                    style={{
                        background: "#ffffff",
                        color: "#111827",
                        borderRadius: "12px",
                        padding: "22px",
                    }}
                >
                    <h2 style={{ marginTop: 0 }}>RFID Information</h2>

                    <div
                        style={{
                            marginTop: "18px",
                            padding: "14px",
                            borderRadius: "9px",
                            background: (assignedRfid || collector.rfid)
                                ? "#ecfdf5"
                                : "#fff7ed",
                            border: `1px solid ${
                                (assignedRfid || collector.rfid) ? "#a7f3d0" : "#fed7aa"
                            }`,
                        }}
                    >
                        <div
                            style={{
                                color: "#6b7280",
                                fontSize: "13px",
                                marginBottom: "5px",
                            }}
                        >
                            RFID Status
                        </div>

                        <strong>
                            {assignedRfid || collector.rfid
                                ? "Assigned"
                                : "Not Assigned"}
                        </strong>
                    </div>

                    <div style={{ marginTop: "18px" }}>
                        <div
                            style={{
                                color: "#6b7280",
                                fontSize: "13px",
                                marginBottom: "5px",
                            }}
                        >
                            RFID ID
                        </div>

                        <strong>
                            {assignedRfid || collector.rfid || "No RFID assigned"}
                        </strong>
                    </div>

                    <button
                        onClick={() => {
                            setRfidError("");
                            setSelectedRfid("");
                            setRfidInput("");
                            setScanPhase("idle");
                            setScannedUid("");
                            uidClaimedRef.current = false;
                            setShowRfidModal(true);
                        }}
                        style={{
                            marginTop: "20px",
                            width: "100%",
                            padding: "11px",
                            borderRadius: "8px",
                            border: "none",
                            background: "#111827",
                            color: "#ffffff",
                            fontWeight: 700,
                            cursor: "pointer",
                        }}
                    >
                        {assignedRfid || collector.rfid
                            ? "Change RFID"
                            : "+ Assign RFID"}
                    </button>
                </section>
            </div>

            <div
                style={{
                    display: "grid",
                    gridTemplateColumns:
                        "repeat(auto-fit, minmax(190px, 1fr))",
                    gap: "16px",
                    marginBottom: "20px",
                }}
            >
                {[
                    ["Today's Collections", total],
                    ["Segregated", segregated],
                    ["Non-Segregated", nonSegregated],
                    ["Segregation Rate", `${segregationRate}%`],
                ].map(([label, value]) => (
                    <div
                        key={label}
                        style={{
                            background: "#1d2a3a",
                            border: "1px solid #334155",
                            borderRadius: "12px",
                            padding: "20px",
                        }}
                    >
                        <div
                            style={{
                                color: "#94a3b8",
                                fontSize: "12px",
                                textTransform: "uppercase",
                                letterSpacing: "0.04em",
                                marginBottom: "8px",
                            }}
                        >
                            {label}
                        </div>

                        <div
                            style={{
                                fontSize: "28px",
                                fontWeight: 800,
                            }}
                        >
                            {value}
                        </div>
                    </div>
                ))}
            </div>

            <section
                style={{
                    background: "#ffffff",
                    color: "#111827",
                    borderRadius: "12px",
                    padding: "20px",
                    overflow: "hidden",
                }}
            >
                <h2 style={{ marginTop: 0 }}>Recent Collections</h2>

                <div style={{ overflowX: "auto" }}>
                    <table
                        style={{
                            width: "100%",
                            borderCollapse: "collapse",
                            minWidth: "520px",
                        }}
                    >
                        <thead>
                            <tr style={{ background: "#f3f4f6" }}>
                                <th
                                    style={{
                                        textAlign: "left",
                                        padding: "13px",
                                        color: "#64748b",
                                        fontSize: "12px",
                                    }}
                                >
                                    HOUSE
                                </th>
                                <th
                                    style={{
                                        textAlign: "left",
                                        padding: "13px",
                                        color: "#64748b",
                                        fontSize: "12px",
                                    }}
                                >
                                    TIME
                                </th>
                                <th
                                    style={{
                                        textAlign: "left",
                                        padding: "13px",
                                        color: "#64748b",
                                        fontSize: "12px",
                                    }}
                                >
                                    WASTE STATUS
                                </th>
                            </tr>
                        </thead>

                        <tbody>
                            {history.map(([house, time, status]) => (
                                <tr
                                    key={`${house}-${time}`}
                                    style={{
                                        borderBottom:
                                            "1px solid #e5e7eb",
                                    }}
                                >
                                    <td style={{ padding: "15px 13px" }}>
                                        {house}
                                    </td>

                                    <td style={{ padding: "15px 13px" }}>
                                        {time}
                                    </td>

                                    <td style={{ padding: "15px 13px" }}>
                                        <span
                                            style={{
                                                padding: "6px 10px",
                                                borderRadius: "20px",
                                                background:
                                                    status ===
                                                    "Segregated"
                                                        ? "#dcfce7"
                                                        : "#fee2e2",
                                                color:
                                                    status ===
                                                    "Segregated"
                                                        ? "#166534"
                                                        : "#991b1b",
                                                fontWeight: 700,
                                                fontSize: "12px",
                                            }}
                                        >
                                            {status === "Segregated"
                                                ? "🟢 "
                                                : "🔴 "}
                                            {status}
                                        </span>
                                    </td>
                                </tr>
                            ))}

                            {history.length === 0 && (
                                <tr>
                                    <td
                                        colSpan="3"
                                        style={{
                                            padding: "35px",
                                            textAlign: "center",
                                            color: "#6b7280",
                                        }}
                                    >
                                        No collection records available.
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </section>

            {showRfidModal && (
                <div
                    onClick={closeRfidModal}
                    style={{
                        position: "fixed",
                        inset: 0,
                        background: "rgba(0, 0, 0, 0.58)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        padding: "20px",
                        zIndex: 1000,
                    }}
                >
                    <div
                        onClick={(event) => event.stopPropagation()}
                        style={{
                            width: "100%",
                            maxWidth: "500px",
                            background: "#ffffff",
                            color: "#111827",
                            borderRadius: "14px",
                            padding: "24px",
                            boxSizing: "border-box",
                            boxShadow: "0 20px 50px rgba(0,0,0,0.25)",
                        }}
                    >
                        <div
                            style={{
                                display: "flex",
                                justifyContent: "space-between",
                                alignItems: "flex-start",
                            }}
                        >
                            <div>
                                <h2 style={{ margin: 0 }}>
                                    {assignedRfid || collector.rfid
                                        ? "Change RFID"
                                        : "Assign RFID"}
                                </h2>
                                <p
                                    style={{
                                        margin: "6px 0 0",
                                        color: "#6b7280",
                                        fontSize: "13px",
                                    }}
                                >
                                    Assign an RFID card to {collector.name}
                                </p>
                            </div>

                            <button
                                type="button"
                                onClick={closeRfidModal}
                                style={{
                                    border: "none",
                                    background: "#f3f4f6",
                                    borderRadius: "7px",
                                    width: "32px",
                                    height: "32px",
                                    cursor: "pointer",
                                    fontSize: "18px",
                                }}
                            >
                                ×
                            </button>
                        </div>

                        <div
                            style={{
                                marginTop: "20px",
                                padding: "14px",
                                borderRadius: "9px",
                                background: "#f8fafc",
                                border: "1px solid #e5e7eb",
                            }}
                        >
                            <div
                                style={{
                                    color: "#6b7280",
                                    fontSize: "12px",
                                    marginBottom: "4px",
                                }}
                            >
                                Collector
                            </div>
                            <strong>{collector.name}</strong>
                            <div
                                style={{
                                    color: "#6b7280",
                                    fontSize: "13px",
                                    marginTop: "3px",
                                }}
                            >
                                {collector.id} · {collector.area}
                            </div>
                        </div>

                        {/* ---- Scan the card, do not type it ------------- */}
                        <div
                            style={{
                                marginTop: "18px",
                                padding: "16px",
                                borderRadius: "10px",
                                border:
                                    scanPhase === "success"
                                        ? "1px solid #a7f3d0"
                                        : scanPhase === "waiting"
                                        ? "1px solid #bfdbfe"
                                        : "1px solid #e5e7eb",
                                background:
                                    scanPhase === "success"
                                        ? "#ecfdf5"
                                        : scanPhase === "waiting"
                                        ? "#eff6ff"
                                        : "#f8fafc",
                            }}
                        >
                            <div
                                style={{
                                    fontSize: "12px",
                                    fontWeight: 700,
                                    color: "#6b7280",
                                    letterSpacing: "0.4px",
                                }}
                            >
                                SCAN CARD
                            </div>

                            {scanPhase === "idle" && (
                                <>
                                    <div
                                        style={{
                                            margin: "8px 0 12px",
                                            fontSize: "13px",
                                            color: "#4b5563",
                                        }}
                                    >
                                        Hold the card on the RC522 reader after
                                        pressing the button. The UID is read and
                                        assigned to {collector.name}{" "}
                                        automatically.
                                    </div>
                                    <button
                                        type="button"
                                        onClick={beginScan}
                                        disabled={savingRfid}
                                        style={{
                                            width: "100%",
                                            padding: "12px",
                                            borderRadius: "8px",
                                            border: "none",
                                            background: "#2563eb",
                                            color: "#ffffff",
                                            fontWeight: 700,
                                            fontSize: "14px",
                                            cursor: savingRfid
                                                ? "not-allowed"
                                                : "pointer",
                                            opacity: savingRfid ? 0.7 : 1,
                                        }}
                                    >
                                        Scan RFID Card
                                    </button>
                                </>
                            )}

                            {scanPhase === "arming" && (
                                <div
                                    style={{
                                        marginTop: "8px",
                                        fontSize: "14px",
                                        color: "#1d4ed8",
                                        fontWeight: 600,
                                    }}
                                >
                                    Connecting to reader…
                                </div>
                            )}

                            {scanPhase === "waiting" && (
                                <>
                                    <div
                                        style={{
                                            margin: "8px 0 12px",
                                            fontSize: "15px",
                                            color: "#1d4ed8",
                                            fontWeight: 700,
                                        }}
                                    >
                                        Waiting for RFID card…
                                    </div>
                                    <div
                                        style={{
                                            fontSize: "12px",
                                            color: "#6b7280",
                                            marginBottom: "12px",
                                        }}
                                    >
                                        Reader: {ESP32_BASE_URL}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={cancelScan}
                                        style={{
                                            width: "100%",
                                            padding: "11px",
                                            borderRadius: "8px",
                                            border: "1px solid #bfdbfe",
                                            background: "#ffffff",
                                            color: "#1d4ed8",
                                            fontWeight: 700,
                                            cursor: "pointer",
                                        }}
                                    >
                                        Cancel scan
                                    </button>
                                </>
                            )}

                            {(scanPhase === "detected" ||
                                scanPhase === "assigning") && (
                                <>
                                    <div
                                        style={{
                                            margin: "8px 0 4px",
                                            fontSize: "12px",
                                            color: "#6b7280",
                                        }}
                                    >
                                        Card detected
                                    </div>
                                    <div
                                        style={{
                                            fontFamily:
                                                "ui-monospace, SFMono-Regular, Menlo, monospace",
                                            fontSize: "18px",
                                            fontWeight: 700,
                                            color: "#111827",
                                            wordBreak: "break-all",
                                        }}
                                    >
                                        {scannedUid}
                                    </div>
                                    <div
                                        style={{
                                            marginTop: "8px",
                                            fontSize: "13px",
                                            color: "#4b5563",
                                        }}
                                    >
                                        Assigning to {collector.name}…
                                    </div>
                                </>
                            )}

                            {scanPhase === "success" && (
                                <>
                                    <div
                                        style={{
                                            margin: "8px 0 4px",
                                            fontSize: "15px",
                                            fontWeight: 700,
                                            color: "#065f46",
                                        }}
                                    >
                                        ✓ RFID assigned
                                    </div>
                                    <div
                                        style={{
                                            fontFamily:
                                                "ui-monospace, SFMono-Regular, Menlo, monospace",
                                            fontSize: "16px",
                                            color: "#047857",
                                            wordBreak: "break-all",
                                        }}
                                    >
                                        {scannedUid} → {collector.name}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={closeRfidModal}
                                        style={{
                                            marginTop: "12px",
                                            width: "100%",
                                            padding: "11px",
                                            borderRadius: "8px",
                                            border: "none",
                                            background: "#059669",
                                            color: "#ffffff",
                                            fontWeight: 700,
                                            cursor: "pointer",
                                        }}
                                    >
                                        Done
                                    </button>
                                </>
                            )}

                            {scanPhase === "error" && (
                                <>
                                    <div
                                        style={{
                                            margin: "8px 0 12px",
                                            fontSize: "13px",
                                            color: "#991b1b",
                                            fontWeight: 600,
                                        }}
                                    >
                                        {scannedUid
                                            ? `Card ${scannedUid} was not assigned.`
                                            : "Scan did not complete."}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={beginScan}
                                        disabled={savingRfid}
                                        style={{
                                            width: "100%",
                                            padding: "11px",
                                            borderRadius: "8px",
                                            border: "none",
                                            background: "#2563eb",
                                            color: "#ffffff",
                                            fontWeight: 700,
                                            cursor: savingRfid
                                                ? "not-allowed"
                                                : "pointer",
                                            opacity: savingRfid ? 0.7 : 1,
                                        }}
                                    >
                                        Retry scan
                                    </button>
                                </>
                            )}
                        </div>

                        <div
                            style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "10px",
                                margin: "18px 0 0",
                                color: "#9ca3af",
                                fontSize: "12px",
                            }}
                        >
                            <span
                                style={{
                                    flex: 1,
                                    height: "1px",
                                    background: "#e5e7eb",
                                }}
                            />
                            OR ENTER MANUALLY
                            <span
                                style={{
                                    flex: 1,
                                    height: "1px",
                                    background: "#e5e7eb",
                                }}
                            />
                        </div>

                        <label
                            style={{
                                display: "block",
                                marginTop: "18px",
                                fontWeight: 700,
                                marginBottom: "7px",
                            }}
                        >
                            RFID ID
                        </label>

                        <input
                            value={rfidInput}
                            disabled={scanBusy}
                            onChange={(event) => {
                                setRfidInput(event.target.value.toUpperCase());
                                setSelectedRfid("");
                                setRfidError("");
                            }}
                            placeholder="Enter RFID ID (RFID-00000)"
                            style={{
                                width: "100%",
                                padding: "12px",
                                border: "1px solid #d1d5db",
                                borderRadius: "8px",
                                boxSizing: "border-box",
                                fontSize: "14px",
                            }}
                        />

                        <div
                            style={{
                                marginTop: "18px",
                                fontWeight: 700,
                                fontSize: "14px",
                            }}
                        >
                            Available RFID
                        </div>

                        <div
                            style={{
                                marginTop: "8px",
                                display: "grid",
                                gap: "8px",
                            }}
                        >
                            {Array.isArray(rfids)
                                ? rfids
                                .filter(
                                    (item) =>
                                        item.status === "AVAILABLE" ||
                                        item.rfid_id ===
                                            (assignedRfid || collector.rfid)
                                )
                                .map(
                                (rfid) => (
                                    <button
                                        key={rfid.rfid_id}
                                        type="button"
                                        disabled={scanBusy}
                                        onClick={() => {
                                            setSelectedRfid(rfid.rfid_id);
                                            setRfidInput(rfid.rfid_id);
                                            setRfidError("");
                                        }}
                                        style={{
                                            width: "100%",
                                            padding: "11px 13px",
                                            borderRadius: "8px",
                                            border:
                                                selectedRfid === rfid.rfid_id
                                                    ? "2px solid #2563eb"
                                                    : "1px solid #e5e7eb",
                                            background:
                                                selectedRfid === rfid.rfid_id
                                                    ? "#eff6ff"
                                                    : "#ffffff",
                                            color: "#111827",
                                            display: "flex",
                                            justifyContent: "space-between",
                                            cursor: "pointer",
                                            fontWeight: 600,
                                            boxSizing: "border-box",
                                        }}
                                    >
                                        <span>{rfid.rfid_id}</span>
                                        {selectedRfid === rfid.rfid_id && (
                                            <span style={{ color: "#2563eb" }}>
                                                ✓
                                            </span>
                                        )}
                                    </button>
                                )
                            ): null}
                        </div>

                        <div
                            style={{
                                marginTop: "18px",
                                padding: "13px",
                                borderRadius: "8px",
                                background: "#ecfdf5",
                                border: "1px solid #a7f3d0",
                            }}
                        >
                            <div
                                style={{
                                    color: "#047857",
                                    fontSize: "12px",
                                    marginBottom: "3px",
                                    fontWeight: 700,
                                }}
                            >
                                INITIAL WASTE STATUS
                            </div>
                            <strong style={{ color: "#065f46" }}>
                                🟢 Segregated
                            </strong>
                            <div
                                style={{
                                    marginTop: "4px",
                                    color: "#047857",
                                    fontSize: "12px",
                                }}
                            >
                                New RFID assignments start as segregated.
                            </div>
                        </div>

                        {rfidError && (
                            <div
                                style={{
                                    marginTop: "12px",
                                    padding: "10px",
                                    borderRadius: "7px",
                                    background: "#fee2e2",
                                    color: "#991b1b",
                                    fontSize: "13px",
                                }}
                            >
                                {rfidError}
                            </div>
                        )}

                        <div
                            style={{
                                display: "flex",
                                justifyContent: "flex-end",
                                gap: "10px",
                                marginTop: "20px",
                            }}
                        >
                            <button
                                type="button"
                                onClick={closeRfidModal}
                                style={{
                                    padding: "10px 15px",
                                    borderRadius: "8px",
                                    border: "1px solid #d1d5db",
                                    background: "#ffffff",
                                    color: "#111827",
                                    fontWeight: 700,
                                    cursor: "pointer",
                                }}
                            >
                                Cancel
                            </button>

                            <button
                                type="button"
                                disabled={scanBusy}
                                onClick={async () => {
                                    const value = (
                                        selectedRfid || rfidInput
                                    ).trim().toUpperCase();

                                    if (!value) {
                                        setRfidError(
                                            "Please select or enter an RFID ID."
                                        );
                                        return;
                                    }

                                    if (!/^RFID-\d{5}$/i.test(value)) {
                                        setRfidError(
                                            "RFID must use the format RFID-00000."
                                        );
                                        return;
                                    }

                                    try {
                                        setSavingRfid(true);
                                        setRfidError("");

                                        const params = new URLSearchParams({
                                            collector_id: collector.id,
                                            rfid_id: value,
                                        });

                                        const response = await fetch(
                                            `${API_BASE}/rfids/assign?${params.toString()}`,
                                            {
                                                method: "POST",
                                            }
                                        );

                                        const data = await response.json();

                                        if (!response.ok) {
                                            throw new Error(
                                                data.detail ||
                                                    "Failed to assign RFID"
                                            );
                                        }

                                        setAssignedRfid(data.rfid_id);
                                        setCollector((current) => ({
                                            ...current,
                                            rfid: data.rfid_id,
                                            waste_state:
                                                data.waste_state,
                                        }));

                                        setRfids((current) =>
                                            current.map((item) => {
                                                if (
                                                    item.rfid_id ===
                                                    data.rfid_id
                                                ) {
                                                    return {
                                                        ...item,
                                                        collector_id:
                                                            data.collector_id,
                                                        status: "ASSIGNED",
                                                        waste_state:
                                                            data.waste_state,
                                                        assigned_at:
                                                            data.assigned_at,
                                                    };
                                                }

                                                if (
                                                    item.collector_id ===
                                                        collector.id &&
                                                    item.rfid_id !==
                                                        data.rfid_id
                                                ) {
                                                    return {
                                                        ...item,
                                                        collector_id: null,
                                                        status: "AVAILABLE",
                                                    };
                                                }

                                                return item;
                                            })
                                        );

                                        closeRfidModal();
                                    } catch (error) {
                                        console.error(
                                            "RFID assignment failed:",
                                            error
                                        );
                                        setRfidError(
                                            error.message ||
                                                "Failed to assign RFID."
                                        );
                                    } finally {
                                        setSavingRfid(false);
                                    }
                                }}
                                style={{
                                    padding: "10px 15px",
                                    borderRadius: "8px",
                                    border: "none",
                                    background: "#2563eb",
                                    color: "#ffffff",
                                    fontWeight: 700,
                                    cursor: scanBusy
                                        ? "not-allowed"
                                        : "pointer",
                                    opacity: scanBusy ? 0.7 : 1,
                                }}
                            >
                                {savingRfid
                                    ? "Saving..."
                                    : assignedRfid || collector.rfid
                                    ? "Change RFID"
                                    : "Assign RFID"}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

export default CollectorProfile;
