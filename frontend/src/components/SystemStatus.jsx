import { useEffect, useState } from "react";
import { getJSON } from "../api";

const CHIP = {
    display: "inline-block",
    padding: "3px 9px",
    borderRadius: "999px",
    fontSize: "12px",
    fontWeight: 700,
};

function Chip({ ok, yes = "OK", no = "OFF" }) {
    return (
        <span
            style={{
                ...CHIP,
                background: ok ? "#dcfce7" : "#f3f4f6",
                color: ok ? "#166534" : "#6b7280",
            }}
        >
            {ok ? yes : no}
        </span>
    );
}

function Row({ label, children }) {
    return (
        <div
            style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "6px 0",
                fontSize: "13px",
            }}
        >
            <span style={{ color: "#4b5563" }}>{label}</span>
            <span>{children}</span>
        </div>
    );
}

/**
 * Subsystem diagnostics: camera, depth, detector, RFID bindings and location.
 *
 * Every one of these can be independently unavailable, and the old dashboard
 * showed none of them — a camera that never opened looked identical to one
 * with nobody in frame.
 */
function SystemStatus() {
    const [health, setHealth] = useState(null);
    const [bindings, setBindings] = useState([]);
    const [error, setError] = useState("");

    useEffect(() => {
        let cancelled = false;

        const poll = async () => {
            try {
                const [healthData, bindingData] = await Promise.all([
                    getJSON("/health"),
                    getJSON("/worker-bindings"),
                ]);
                if (cancelled) return;
                setHealth(healthData);
                setBindings(bindingData.bindings || []);
                setError("");
            } catch (err) {
                if (!cancelled) setError(String(err));
            }
        };

        poll();
        const interval = setInterval(poll, 2000);
        return () => {
            cancelled = true;
            clearInterval(interval);
        };
    }, []);

    if (error && !health) {
        return (
            <div
                style={{
                    background: "#ffffff",
                    borderRadius: "12px",
                    padding: "16px",
                    color: "#b91c1c",
                }}
            >
                Backend unreachable: {error}
            </div>
        );
    }

    if (!health) {
        return null;
    }

    const location = health.location?.current;

    return (
        <div
            style={{
                background: "#ffffff",
                borderRadius: "12px",
                padding: "16px",
                color: "#111827",
            }}
        >
            <h3 style={{ marginTop: 0 }}>System</h3>

            <Row label="Camera">
                <Chip ok={health.camera?.open} yes="CONNECTED" no="NOT CONNECTED" />
            </Row>
            <Row label="Backend">{health.camera?.backend}</Row>
            <Row label="Depth">
                <Chip
                    ok={health.capabilities?.depth}
                    yes="AVAILABLE"
                    no="UNAVAILABLE"
                />
            </Row>
            <Row label="Detector">
                <Chip ok={health.detector?.loaded} yes="LOADED" no="NOT LOADED" />
            </Row>
            {health.camera?.last_error && (
                <div style={{ color: "#b91c1c", fontSize: "12px" }}>
                    {health.camera.last_error}
                </div>
            )}
            {health.detector?.error && (
                <div style={{ color: "#b91c1c", fontSize: "12px" }}>
                    {health.detector.error}
                </div>
            )}

            <hr />

            <Row label="RFID zone">
                <Chip
                    ok={health.identity?.zone_configured}
                    yes="CONFIGURED"
                    no="NOT SET"
                />
            </Row>
            <Row label="Session">
                <code style={{ fontSize: "12px" }}>{health.identity?.session_id}</code>
            </Row>

            <h4 style={{ marginBottom: "4px" }}>
                Worker bindings ({bindings.length})
            </h4>
            {bindings.length === 0 ? (
                <p style={{ color: "#6b7280", fontSize: "13px" }}>
                    No worker is bound. Tap an assigned RFID card at the reader.
                </p>
            ) : (
                bindings.map((binding) => (
                    <div
                        key={binding.collector_id}
                        style={{
                            padding: "8px 10px",
                            marginBottom: "6px",
                            borderRadius: "8px",
                            background: "#ecfdf5",
                            border: "1px solid #a7f3d0",
                            fontSize: "13px",
                        }}
                    >
                        <strong>{binding.collector_id}</strong> → track #
                        {binding.track_id}
                        <div style={{ color: "#4b5563", fontSize: "12px" }}>
                            RFID {binding.rfid_id} ·{" "}
                            {Math.round(binding.identity_confidence * 100)}% confidence
                        </div>
                    </div>
                ))
            )}

            <hr />

            <Row label="Location source">
                {location ? location.source : "—"}
            </Row>
            <Row label="Position">
                {location
                    ? `${location.latitude.toFixed(5)}, ${location.longitude.toFixed(5)}`
                    : "no fix"}
            </Row>
            <Row label="Accuracy">
                {location?.accuracy_m != null ? `± ${location.accuracy_m} m` : "—"}
            </Row>
            <Row label="Fix age">
                {location ? `${location.age_s}s${location.stale ? " (stale)" : ""}` : "—"}
            </Row>
            <Row label="Heading">
                <span style={{ color: "#6b7280" }}>unavailable (no IMU)</span>
            </Row>
        </div>
    );
}

export default SystemStatus;
