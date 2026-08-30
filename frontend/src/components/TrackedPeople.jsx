/**
 * Every tracked person, with authorised pickers visually distinguished.
 *
 * Unidentified people are deliberately still shown. The system tracks
 * everybody and only *authorises* those with an RFID binding; hiding
 * pedestrians would make it impossible to see why a binding was ambiguous.
 */
function TrackedPeople({ people }) {
    const authorized = people.filter((p) => p.is_authorized_picker).length;

    return (
        <div
            style={{
                width: "100%",
                minWidth: 0,
                maxHeight: "600px",
                overflowY: "auto",
                background: "#ffffff",
                borderRadius: "12px",
                padding: "18px",
                color: "#111827",
                boxSizing: "border-box",
            }}
        >
            <div
                style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: "6px",
                }}
            >
                <h2 style={{ margin: 0, color: "#111827" }}>Tracked People</h2>
                <span
                    style={{
                        background: "#111827",
                        color: "#ffffff",
                        padding: "5px 10px",
                        borderRadius: "20px",
                        fontSize: "13px",
                        fontWeight: "bold",
                    }}
                >
                    {people.length}
                </span>
            </div>

            <div
                style={{
                    fontSize: "13px",
                    color: "#4b5563",
                    marginBottom: "14px",
                }}
            >
                {authorized} authorised picker{authorized === 1 ? "" : "s"} ·{" "}
                {people.length - authorized} unidentified
            </div>

            {people.length === 0 ? (
                <div
                    style={{
                        padding: "15px",
                        borderRadius: "8px",
                        background: "#f5f5f5",
                        color: "#6b7280",
                    }}
                >
                    No people detected.
                </div>
            ) : (
                people.map((person) => {
                    const isPicker = person.is_authorized_picker;
                    return (
                        <div
                            key={person.track_id}
                            style={{
                                padding: "14px",
                                marginBottom: "10px",
                                borderRadius: "8px",
                                background: isPicker ? "#ecfdf5" : "#f5f5f5",
                                border: `1px solid ${
                                    isPicker ? "#6ee7b7" : "#e5e5e5"
                                }`,
                                color: "#111827",
                            }}
                        >
                            <div
                                style={{
                                    display: "flex",
                                    justifyContent: "space-between",
                                    alignItems: "center",
                                }}
                            >
                                <strong>Track #{person.track_id}</strong>
                                <span
                                    style={{
                                        padding: "3px 9px",
                                        borderRadius: "999px",
                                        fontSize: "11px",
                                        fontWeight: 700,
                                        background: isPicker ? "#059669" : "#e5e7eb",
                                        color: isPicker ? "#ffffff" : "#4b5563",
                                    }}
                                >
                                    {isPicker
                                        ? "AUTHORIZED PICKER"
                                        : "UNIDENTIFIED PERSON"}
                                </span>
                            </div>

                            {isPicker && (
                                <div
                                    style={{
                                        marginTop: "8px",
                                        fontSize: "13px",
                                        fontWeight: 600,
                                    }}
                                >
                                    {person.collector_id}
                                    {person.identity_confidence != null && (
                                        <span
                                            style={{
                                                color: "#4b5563",
                                                fontWeight: 400,
                                            }}
                                        >
                                            {" "}
                                            · identity{" "}
                                            {Math.round(
                                                person.identity_confidence * 100
                                            )}
                                            %
                                        </span>
                                    )}
                                </div>
                            )}

                            <div
                                style={{
                                    marginTop: "6px",
                                    fontSize: "13px",
                                    color: "#4b5563",
                                }}
                            >
                                Detection:{" "}
                                <strong>
                                    {Math.round(
                                        (person.detection_confidence ?? 0) * 100
                                    )}
                                    %
                                </strong>
                            </div>

                            {person.camera_position_m && (
                                <div
                                    style={{
                                        marginTop: "4px",
                                        fontSize: "12px",
                                        color: "#6b7280",
                                    }}
                                >
                                    Depth: x {person.camera_position_m.x} m · y{" "}
                                    {person.camera_position_m.y} m · z{" "}
                                    {person.camera_position_m.z} m
                                </div>
                            )}
                        </div>
                    );
                })
            )}
        </div>
    );
}

export default TrackedPeople;
