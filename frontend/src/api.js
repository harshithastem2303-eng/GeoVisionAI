// Single place the frontend learns where the backend is.
//
// Hardcoding 127.0.0.1 meant a phone could never reach the backend, and the
// phone is the preferred location source. The default below resolves to
// whatever host served the page, so opening the dashboard from a phone at
// http://<laptop-ip>:5173 talks to http://<laptop-ip>:8000 automatically.
//
// Override explicitly with a VITE_API_BASE entry in frontend/.env.local.

const explicit = import.meta.env?.VITE_API_BASE;

export const API_BASE =
    explicit && explicit.trim() !== ""
        ? explicit.trim().replace(/\/$/, "")
        : `${window.location.protocol}//${window.location.hostname}:8000`;

export async function getJSON(path) {
    const response = await fetch(`${API_BASE}${path}`);
    if (!response.ok) {
        throw new Error(`GET ${path} failed: ${response.status}`);
    }
    return response.json();
}

export async function postJSON(path, body) {
    const response = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || `POST ${path} failed: ${response.status}`);
    }
    return data;
}
