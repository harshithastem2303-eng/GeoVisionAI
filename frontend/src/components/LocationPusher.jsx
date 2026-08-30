import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../api";

/**
 * Pushes this browser's geolocation to the backend.
 *
 * This is the whole location architecture: no serial GNSS, no IMU, no native
 * app. Open the dashboard on a phone (source PHONE) or use the laptop
 * browser as a fallback (source LAPTOP) and the fixes arrive at POST
 * /location. Identical on Windows and macOS.
 *
 * Geolocation requires a secure context, so over LAN this works on
 * http://localhost but a phone on http://<laptop-ip>:5173 will be refused by
 * the browser unless the origin is trusted. That limitation is surfaced in
 * the UI rather than hidden.
 */
function LocationPusher({ source = "PHONE" }) {
    const [watching, setWatching] = useState(false);
    const [lastFix, setLastFix] = useState(null);
    const [error, setError] = useState("");
    const watchId = useRef(null);

    const secure = window.isSecureContext;

    useEffect(() => {
        return () => {
            if (watchId.current !== null) {
                navigator.geolocation.clearWatch(watchId.current);
            }
        };
    }, []);

    const push = async (position) => {
        const payload = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy_m: position.coords.accuracy,
            timestamp: position.timestamp,
            source,
        };

        try {
            const response = await fetch(`${API_BASE}/location`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                // A rejected fix is information, not a silent failure.
                setError(data.detail || "Backend rejected the fix");
                return;
            }
            setError("");
            setLastFix(data.fix);
        } catch (err) {
            setError(String(err));
        }
    };

    const start = () => {
        if (!navigator.geolocation) {
            setError("This browser has no Geolocation API");
            return;
        }
        watchId.current = navigator.geolocation.watchPosition(
            push,
            (err) => setError(err.message),
            { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 }
        );
        setWatching(true);
    };

    const stop = () => {
        if (watchId.current !== null) {
            navigator.geolocation.clearWatch(watchId.current);
            watchId.current = null;
        }
        setWatching(false);
    };

    return (
        <div
            style={{
                background: "#ffffff",
                borderRadius: "12px",
                padding: "16px",
                color: "#111827",
                marginTop: "16px",
            }}
        >
            <h3 style={{ marginTop: 0 }}>Location ({source})</h3>

            {!secure && (
                <p style={{ color: "#b45309", fontSize: "13px" }}>
                    Not a secure context — browsers only grant geolocation over
                    HTTPS or localhost.
                </p>
            )}

            <button
                onClick={watching ? stop : start}
                style={{
                    padding: "8px 14px",
                    border: "none",
                    borderRadius: "8px",
                    background: watching ? "#b91c1c" : "#111827",
                    color: "#ffffff",
                    fontWeight: 600,
                    cursor: "pointer",
                }}
            >
                {watching ? "Stop sharing location" : "Share this device's location"}
            </button>

            {lastFix && (
                <div style={{ marginTop: "12px", fontSize: "13px" }}>
                    <div>
                        {lastFix.latitude.toFixed(6)}, {lastFix.longitude.toFixed(6)}
                    </div>
                    <div style={{ color: "#4b5563" }}>
                        ± {lastFix.accuracy_m ?? "?"} m · {lastFix.source}
                    </div>
                    {/* There is no IMU. Saying so beats an empty field. */}
                    <div style={{ color: "#6b7280", fontSize: "12px" }}>
                        Heading unavailable (no IMU)
                    </div>
                </div>
            )}

            {error && (
                <p style={{ color: "#b91c1c", fontSize: "13px", marginBottom: 0 }}>
                    {error}
                </p>
            )}
        </div>
    );
}

export default LocationPusher;
