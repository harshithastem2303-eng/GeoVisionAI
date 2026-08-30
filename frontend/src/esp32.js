// Single place the frontend learns where the RC522 reader's ESP32 lives.
//
// The board is an HTTP *server* exposing /start-scan, /latest and /stop-scan;
// it never posts anywhere. Two different screens now drive it -- the runtime
// tap bridge (components/RFIDScanner.jsx) and the card-enrolment modal in
// pages/CollectorProfile.jsx -- so the address and the three calls live here
// rather than being duplicated, the same way src/api.js owns API_BASE.
//
// The address is configuration, not code: the board takes a fresh DHCP lease
// whenever the network restarts. Set VITE_ESP32_BASE_URL in
// frontend/.env.local; the literal below is only the last known value.
const ESP32_FALLBACK_URL = "http://192.168.0.127";

const configured = import.meta.env?.VITE_ESP32_BASE_URL;

export const ESP32_BASE_URL =
    configured && configured.trim() !== ""
        ? configured.trim().replace(/\/$/, "")
        : ESP32_FALLBACK_URL;

/** Poll cadence for /latest, within the 250-500 ms the reader is happy with. */
export const ESP32_POLL_INTERVAL_MS = 300;

// Cache-busting matters here: /latest is polled hard, and a cached 200 would
// replay a stale UID for as long as the browser held it.
function readerUrl(path) {
    return ESP32_BASE_URL + path + "?t=" + Date.now();
}

async function readerGet(path, signal) {
    const response = await fetch(readerUrl(path), {
        method: "GET",
        mode: "cors",
        cache: "no-store",
        signal,
    });
    if (!response.ok) {
        throw new Error("ESP32 returned HTTP " + response.status);
    }
    return response;
}

/** Arm the reader. Throws if the board is unreachable. */
export async function startScan(signal) {
    await readerGet("/start-scan", signal);
}

/**
 * Read whatever the reader is holding.
 *
 * Returns the UID as a trimmed, upper-cased string, or "" when no card has
 * been presented yet. The board answers {"uid": "", "scan": true} between
 * taps, which is a normal poll result and not an error.
 */
export async function readLatestUid(signal) {
    const response = await readerGet("/latest", signal);
    const data = await response.json();
    return String(data?.uid || "").trim().toUpperCase();
}

/**
 * Disarm the reader. Deliberately never throws: this runs from cleanup paths
 * (modal close, unmount, abort) where a failure to reach the board must not
 * take the caller down with it.
 */
export async function stopScan() {
    try {
        await fetch(readerUrl("/stop-scan"), {
            method: "GET",
            mode: "cors",
            cache: "no-store",
        });
        return true;
    } catch (error) {
        console.warn("Could not stop ESP32 scan:", error);
        return false;
    }
}
