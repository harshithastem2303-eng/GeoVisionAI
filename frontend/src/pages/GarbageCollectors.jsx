import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { API_BASE } from "../api";

function GarbageCollectors() {
    const navigate = useNavigate();

    const [collectors, setCollectors] = useState([]);
    const [search, setSearch] = useState("");
    const [statusFilter, setStatusFilter] = useState("All");
    const [showAddModal, setShowAddModal] = useState(false);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");

    const [newCollector, setNewCollector] = useState({
        name: "",
        phone: "",
        area: "",
        status: "Active",
    });

    const loadCollectors = async () => {
        try {
            setLoading(true);
            setError("");

            const response = await fetch(`${API_BASE}/collectors`);

            if (!response.ok) {
                throw new Error(`Backend returned ${response.status}`);
            }

            const data = await response.json();
            setCollectors(data);
        } catch (err) {
            console.error("Failed to load collectors:", err);
            setError(
                "Could not connect to the GeoVision backend. Make sure FastAPI is running on port 8000."
            );
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadCollectors();
    }, []);

    const filteredCollectors = useMemo(() => {
        const query = search.trim().toLowerCase();

        return collectors.filter((collector) => {
            const matchesSearch =
                !query ||
                collector.name.toLowerCase().includes(query) ||
                collector.id.toLowerCase().includes(query) ||
                (collector.area || "").toLowerCase().includes(query);

            const matchesStatus =
                statusFilter === "All" ||
                collector.status === statusFilter;

            return matchesSearch && matchesStatus;
        });
    }, [collectors, search, statusFilter]);

    const totalCollectors = collectors.length;

    const activeCollectors = collectors.filter(
        (collector) => collector.status === "Active"
    ).length;

    const assignedRfids = collectors.filter(
        (collector) => collector.rfid
    ).length;

    const unassignedRfids = totalCollectors - assignedRfids;

    const generateCollectorId = () => {
        const numbers = collectors
            .map((collector) =>
                Number(String(collector.id).replace("GC", ""))
            )
            .filter((number) => Number.isFinite(number));

        const nextNumber =
            numbers.length > 0 ? Math.max(...numbers) + 1 : 1;

        return `GC${String(nextNumber).padStart(3, "0")}`;
    };

    const handleAddCollector = async (event) => {
        event.preventDefault();

        if (!newCollector.name.trim() || !newCollector.area.trim()) {
            setError("Please enter the collector name and area.");
            return;
        }

        try {
            setSaving(true);
            setError("");

            const collectorId = generateCollectorId();

            const params = new URLSearchParams({
                collector_id: collectorId,
                name: newCollector.name.trim(),
                phone: newCollector.phone.trim(),
                area: newCollector.area.trim(),
                status: newCollector.status,
            });

            const response = await fetch(
                `${API_BASE}/collectors?${params.toString()}`,
                {
                    method: "POST",
                }
            );

            const data = await response.json();

            if (!response.ok) {
                throw new Error(
                    data.detail || "Failed to create collector"
                );
            }

            setCollectors((current) => [...current, data]);

            setNewCollector({
                name: "",
                phone: "",
                area: "",
                status: "Active",
            });

            setShowAddModal(false);
        } catch (err) {
            console.error("Failed to add collector:", err);
            setError(err.message || "Failed to add collector.");
        } finally {
            setSaving(false);
        }
    };

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
                    padding: "24px 28px",
                    marginBottom: "20px",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "20px",
                    flexWrap: "wrap",
                }}
            >
                <div>
                    <h1
                        style={{
                            margin: 0,
                            fontSize: "32px",
                            fontWeight: 800,
                        }}
                    >
                        Garbage Collectors
                    </h1>

                    <p
                        style={{
                            margin: "8px 0 0",
                            color: "#94a3b8",
                            fontSize: "15px",
                        }}
                    >
                        Manage garbage collectors and their RFID assignments
                    </p>
                </div>

                <div style={{ display: "flex", gap: "10px" }}>
                    <button
                        onClick={() => navigate("/")}
                        style={{
                            padding: "11px 16px",
                            borderRadius: "8px",
                            border: "1px solid #475569",
                            background: "#111c2c",
                            color: "#ffffff",
                            fontWeight: 700,
                            cursor: "pointer",
                        }}
                    >
                        ← Dashboard
                    </button>

                    <button
                        onClick={() => {
                            setError("");
                            setShowAddModal(true);
                        }}
                        style={{
                            padding: "11px 16px",
                            borderRadius: "8px",
                            border: "none",
                            background: "#2563eb",
                            color: "#ffffff",
                            fontWeight: 700,
                            cursor: "pointer",
                        }}
                    >
                        + Add Collector
                    </button>
                </div>
            </div>

            {error && (
                <div
                    style={{
                        marginBottom: "18px",
                        padding: "13px 16px",
                        borderRadius: "9px",
                        background: "#fee2e2",
                        color: "#991b1b",
                        border: "1px solid #fecaca",
                    }}
                >
                    {error}
                </div>
            )}

            <div
                style={{
                    display: "grid",
                    gridTemplateColumns:
                        "repeat(auto-fit, minmax(210px, 1fr))",
                    gap: "16px",
                    marginBottom: "20px",
                }}
            >
                {[
                    ["Total Collectors", totalCollectors],
                    ["Active Collectors", activeCollectors],
                    ["RFID Assigned", assignedRfids],
                    ["RFID Unassigned", unassignedRfids],
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
                                fontSize: "13px",
                                marginBottom: "8px",
                                textTransform: "uppercase",
                                letterSpacing: "0.04em",
                            }}
                        >
                            {label}
                        </div>

                        <div
                            style={{
                                fontSize: "30px",
                                fontWeight: 800,
                            }}
                        >
                            {value}
                        </div>
                    </div>
                ))}
            </div>

            <div
                style={{
                    background: "#ffffff",
                    color: "#111827",
                    borderRadius: "12px",
                    padding: "20px",
                    overflow: "hidden",
                }}
            >
                <div
                    style={{
                        display: "flex",
                        gap: "12px",
                        marginBottom: "18px",
                        flexWrap: "wrap",
                    }}
                >
                    <input
                        value={search}
                        onChange={(event) => setSearch(event.target.value)}
                        placeholder="🔍 Search collector, ID or area..."
                        style={{
                            flex: "1 1 280px",
                            minWidth: 0,
                            padding: "12px 14px",
                            border: "1px solid #d1d5db",
                            borderRadius: "8px",
                            fontSize: "14px",
                            outline: "none",
                            boxSizing: "border-box",
                        }}
                    />

                    <select
                        value={statusFilter}
                        onChange={(event) =>
                            setStatusFilter(event.target.value)
                        }
                        style={{
                            padding: "12px 14px",
                            border: "1px solid #d1d5db",
                            borderRadius: "8px",
                            background: "#ffffff",
                            color: "#111827",
                            fontSize: "14px",
                        }}
                    >
                        <option value="All">All Status</option>
                        <option value="Active">Active</option>
                        <option value="Inactive">Inactive</option>
                    </select>
                </div>

                {loading ? (
                    <div
                        style={{
                            padding: "50px",
                            textAlign: "center",
                            color: "#6b7280",
                        }}
                    >
                        Loading garbage collectors...
                    </div>
                ) : (
                    <div style={{ overflowX: "auto" }}>
                        <table
                            style={{
                                width: "100%",
                                borderCollapse: "collapse",
                                minWidth: "760px",
                            }}
                        >
                            <thead>
                                <tr style={{ background: "#f3f4f6" }}>
                                    {[
                                        "Collector",
                                        "ID",
                                        "Area",
                                        "RFID",
                                        "Status",
                                        "Action",
                                    ].map((heading) => (
                                        <th
                                            key={heading}
                                            style={{
                                                textAlign: "left",
                                                padding: "13px 14px",
                                                fontSize: "12px",
                                                color: "#64748b",
                                                textTransform: "uppercase",
                                                letterSpacing: "0.04em",
                                            }}
                                        >
                                            {heading}
                                        </th>
                                    ))}
                                </tr>
                            </thead>

                            <tbody>
                                {filteredCollectors.map((collector) => (
                                    <tr
                                        key={collector.id}
                                        style={{
                                            borderBottom:
                                                "1px solid #e5e7eb",
                                        }}
                                    >
                                        <td style={{ padding: "15px 14px" }}>
                                            <div
                                                style={{
                                                    display: "flex",
                                                    alignItems: "center",
                                                    gap: "10px",
                                                }}
                                            >
                                                <div
                                                    style={{
                                                        width: "38px",
                                                        height: "38px",
                                                        borderRadius: "50%",
                                                        background: "#e5e7eb",
                                                        display: "flex",
                                                        alignItems:
                                                            "center",
                                                        justifyContent:
                                                            "center",
                                                        fontSize: "18px",
                                                    }}
                                                >
                                                    👤
                                                </div>

                                                <strong>
                                                    {collector.name}
                                                </strong>
                                            </div>
                                        </td>

                                        <td style={{ padding: "15px 14px" }}>
                                            {collector.id}
                                        </td>

                                        <td style={{ padding: "15px 14px" }}>
                                            {collector.area || "—"}
                                        </td>

                                        <td style={{ padding: "15px 14px" }}>
                                            {collector.rfid ? (
                                                <span
                                                    style={{
                                                        padding: "5px 9px",
                                                        borderRadius: "6px",
                                                        background:
                                                            "#ecfdf5",
                                                        color: "#047857",
                                                        fontWeight: 700,
                                                        fontSize: "13px",
                                                    }}
                                                >
                                                    {collector.rfid}
                                                </span>
                                            ) : (
                                                <span
                                                    style={{
                                                        color: "#9ca3af",
                                                        fontStyle: "italic",
                                                    }}
                                                >
                                                    Not assigned
                                                </span>
                                            )}
                                        </td>

                                        <td style={{ padding: "15px 14px" }}>
                                            <span
                                                style={{
                                                    padding: "5px 10px",
                                                    borderRadius: "20px",
                                                    background:
                                                        collector.status ===
                                                        "Active"
                                                            ? "#dcfce7"
                                                            : "#fee2e2",
                                                    color:
                                                        collector.status ===
                                                        "Active"
                                                            ? "#166534"
                                                            : "#991b1b",
                                                    fontSize: "12px",
                                                    fontWeight: 700,
                                                }}
                                            >
                                                {collector.status}
                                            </span>
                                        </td>

                                        <td style={{ padding: "15px 14px" }}>
                                            <button
                                                onClick={() =>
                                                    navigate(
                                                        `/garbage-collectors/${collector.id}`
                                                    )
                                                }
                                                style={{
                                                    padding: "8px 13px",
                                                    borderRadius: "7px",
                                                    border: "none",
                                                    background: "#111827",
                                                    color: "#ffffff",
                                                    fontWeight: 700,
                                                    cursor: "pointer",
                                                }}
                                            >
                                                View Profile
                                            </button>
                                        </td>
                                    </tr>
                                ))}

                                {filteredCollectors.length === 0 && (
                                    <tr>
                                        <td
                                            colSpan="6"
                                            style={{
                                                padding: "35px",
                                                textAlign: "center",
                                                color: "#6b7280",
                                            }}
                                        >
                                            No garbage collectors found.
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {showAddModal && (
                <div
                    onClick={() => !saving && setShowAddModal(false)}
                    style={{
                        position: "fixed",
                        inset: 0,
                        background: "rgba(0, 0, 0, 0.55)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        padding: "20px",
                        zIndex: 1000,
                    }}
                >
                    <form
                        onSubmit={handleAddCollector}
                        onClick={(event) => event.stopPropagation()}
                        style={{
                            width: "100%",
                            maxWidth: "480px",
                            background: "#ffffff",
                            color: "#111827",
                            borderRadius: "14px",
                            padding: "24px",
                            boxSizing: "border-box",
                        }}
                    >
                        <h2 style={{ marginTop: 0 }}>Add Collector</h2>

                        <label
                            style={{
                                display: "block",
                                fontWeight: 700,
                                marginBottom: "6px",
                            }}
                        >
                            Full Name
                        </label>

                        <input
                            value={newCollector.name}
                            onChange={(event) =>
                                setNewCollector({
                                    ...newCollector,
                                    name: event.target.value,
                                })
                            }
                            placeholder="Enter collector name"
                            style={{
                                width: "100%",
                                padding: "11px",
                                marginBottom: "14px",
                                border: "1px solid #d1d5db",
                                borderRadius: "7px",
                                boxSizing: "border-box",
                            }}
                        />

                        <label
                            style={{
                                display: "block",
                                fontWeight: 700,
                                marginBottom: "6px",
                            }}
                        >
                            Phone
                        </label>

                        <input
                            value={newCollector.phone}
                            onChange={(event) =>
                                setNewCollector({
                                    ...newCollector,
                                    phone: event.target.value,
                                })
                            }
                            placeholder="+91 XXXXX XXXXX"
                            style={{
                                width: "100%",
                                padding: "11px",
                                marginBottom: "14px",
                                border: "1px solid #d1d5db",
                                borderRadius: "7px",
                                boxSizing: "border-box",
                            }}
                        />

                        <label
                            style={{
                                display: "block",
                                fontWeight: 700,
                                marginBottom: "6px",
                            }}
                        >
                            Area / Ward
                        </label>

                        <input
                            value={newCollector.area}
                            onChange={(event) =>
                                setNewCollector({
                                    ...newCollector,
                                    area: event.target.value,
                                })
                            }
                            placeholder="e.g. Ward 12"
                            style={{
                                width: "100%",
                                padding: "11px",
                                marginBottom: "14px",
                                border: "1px solid #d1d5db",
                                borderRadius: "7px",
                                boxSizing: "border-box",
                            }}
                        />

                        <label
                            style={{
                                display: "block",
                                fontWeight: 700,
                                marginBottom: "6px",
                            }}
                        >
                            Status
                        </label>

                        <select
                            value={newCollector.status}
                            onChange={(event) =>
                                setNewCollector({
                                    ...newCollector,
                                    status: event.target.value,
                                })
                            }
                            style={{
                                width: "100%",
                                padding: "11px",
                                marginBottom: "20px",
                                border: "1px solid #d1d5db",
                                borderRadius: "7px",
                                background: "#ffffff",
                            }}
                        >
                            <option value="Active">Active</option>
                            <option value="Inactive">Inactive</option>
                        </select>

                        <div
                            style={{
                                display: "flex",
                                justifyContent: "flex-end",
                                gap: "10px",
                            }}
                        >
                            <button
                                type="button"
                                disabled={saving}
                                onClick={() => setShowAddModal(false)}
                                style={{
                                    padding: "10px 15px",
                                    borderRadius: "7px",
                                    border: "1px solid #d1d5db",
                                    background: "#ffffff",
                                    cursor: saving
                                        ? "not-allowed"
                                        : "pointer",
                                    fontWeight: 700,
                                }}
                            >
                                Cancel
                            </button>

                            <button
                                type="submit"
                                disabled={saving}
                                style={{
                                    padding: "10px 15px",
                                    borderRadius: "7px",
                                    border: "none",
                                    background: "#2563eb",
                                    color: "#ffffff",
                                    cursor: saving
                                        ? "not-allowed"
                                        : "pointer",
                                    fontWeight: 700,
                                    opacity: saving ? 0.7 : 1,
                                }}
                            >
                                {saving
                                    ? "Saving..."
                                    : "Add Collector"}
                            </button>
                        </div>
                    </form>
                </div>
            )}
        </div>
    );
}



export default GarbageCollectors; 

