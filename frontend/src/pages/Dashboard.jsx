import { useState, useEffect } from "react";

import Header from "../components/Header";
import CameraPreview from "../components/CameraPreview";
import StatusCards from "../components/StatusCards";
import ControlPanel from "../components/ControlPanel";
import TrackedPeople from "../components/TrackedPeople";
import SystemStatus from "../components/SystemStatus";
import LocationPusher from "../components/LocationPusher";

import { API_BASE } from "../api";


function Dashboard() {

    // ==========================================
    // CAMERA STATE
    // ==========================================

    const [cameraConnected, setCameraConnected] = useState(false);

    const [streaming, setStreaming] = useState(false);

    const [fps, setFps] = useState(0);

    const [resolution, setResolution] = useState("640 × 480");

    const [recording, setRecording] = useState(false);

    const [duration, setDuration] = useState("00:00:00");

    const [frames, setFrames] = useState(0);

    const [previewMessage, setPreviewMessage] = useState(
        "Camera is currently inactive"
    );


    // ==========================================
    // PEOPLE TRACKING STATE
    // ==========================================

    const [people, setPeople] = useState([]);


    // ==========================================
    // FETCH CAMERA STATS
    // ==========================================

    useEffect(() => {

        if (!streaming) return;


        const fetchStats = async () => {

            try {

                const response = await fetch(
                    `${API_BASE}/stats`
                );

                const data = await response.json();


                setFps(data.fps);

                setResolution(data.resolution);

                setRecording(data.recording);

                setDuration(data.duration);

                setFrames(data.frames);

            }

            catch (err) {

                console.error(
                    "STATS ERROR:",
                    err
                );

            }

        };


        fetchStats();


        const interval = setInterval(
            fetchStats,
            1000
        );


        return () =>
            clearInterval(interval);

    }, [streaming]);


    // ==========================================
    // FETCH TRACKED PEOPLE
    // ==========================================

    useEffect(() => {

        if (!streaming) return;


        const fetchPeople = async () => {

            try {

                const response = await fetch(
                    `${API_BASE}/people`
                );

                const data = await response.json();


                setPeople(
                    data.people || []
                );

            }

            catch (err) {

                console.error(
                    "PEOPLE ERROR:",
                    err
                );

            }

        };


        fetchPeople();


        const interval = setInterval(
            fetchPeople,
            500
        );


        return () =>
            clearInterval(interval);

    }, [streaming]);


    // ==========================================
    // CONNECT CAMERA
    // ==========================================

    const handleConnect = async () => {

        try {

            const response = await fetch(
                `${API_BASE}/connect`,
                {
                    method: "POST"
                }
            );


            if (!response.ok) {

                throw new Error(
                    "Connection failed"
                );

            }


            setCameraConnected(true);

            setPreviewMessage(
                "Camera Connected"
            );

        }

        catch (err) {

            console.error(
                "CONNECT ERROR:",
                err
            );

            alert(
                "Failed to connect camera"
            );

        }

    };


    // ==========================================
    // START STREAMING
    // ==========================================

    const handleStartStreaming = async () => {

        try {

            const response = await fetch(
                `${API_BASE}/start`,
                {
                    method: "POST"
                }
            );


            if (!response.ok) {

                throw new Error(
                    "Failed to start streaming"
                );

            }


            setRecording(true);

            setStreaming(true);

            setPreviewMessage(
                "Streaming..."
            );

        }

        catch (err) {

            console.error(err);

            alert(
                "Failed to start streaming"
            );

        }

    };


    // ==========================================
    // STOP STREAMING
    // ==========================================

    const handleStopStreaming = async () => {

        try {

            const response = await fetch(
                `${API_BASE}/stop`,
                {
                    method: "POST"
                }
            );


            if (!response.ok) {

                throw new Error(
                    "Failed to stop"
                );

            }


            await response.json();


            setRecording(false);

            setStreaming(false);

            setPeople([]);

            setPreviewMessage(
                "Camera Stopped"
            );

        }

        catch (err) {

            console.error(
                "STOP ERROR:",
                err
            );

        }

    };


    // ==========================================
    // UI
    // ==========================================

    return (

        <div className="dashboard">

            <Header />

            <div
                style={{
                    display: "flex",
                    justifyContent: "flex-end",
                    padding: "12px 24px 0"
                }}
            >
                <button
                    onClick={() => {
                        window.location.href = "/garbage-collectors";
                    }}
                    style={{
                        padding: "10px 16px",
                        border: "none",
                        borderRadius: "8px",
                        background: "#111827",
                        color: "#ffffff",
                        fontWeight: "600",
                        cursor: "pointer"
                    }}
                >
                    👷 Garbage Collectors
                </button>
            </div>


            <div className="content">


                <div
                style={{
                    display: "grid",
                    gridTemplateColumns: "280px minmax(0, 1fr) 280px",
                    gap: "20px",
                    width: "100%",
                    alignItems: "stretch"
                }}
            >
                                    


                    {/* =================================
                        LEFT — TRACKED PEOPLE
                        Authorised pickers are highlighted; unidentified
                        people stay visible on purpose.
                    ================================= */}

                    <TrackedPeople people={people} />


                    {/* =================================
                        CENTER — CAMERA
                    ================================= */}

                    <div
                        
    style={{
        width: "100%",
        minWidth: 0
    }}
>

                        <CameraPreview
                            previewMessage={
                                previewMessage
                            }
                            recording={
                                recording
                            }
                        />

                    </div>


                    {/* =================================
                        RIGHT — SUBSYSTEM DIAGNOSTICS
                    ================================= */}

                    <div style={{ width: "100%", minWidth: 0 }}>

                        <SystemStatus />

                        <LocationPusher source="PHONE" />

                    </div>

                </div>

            </div>


            {/* =================================
                STATUS
            ================================= */}

            <StatusCards
                fps={fps}
                resolution={resolution}
                recording={recording}
                duration={duration}
                frames={frames}
            />


            {/* =================================
                CONTROLS
            ================================= */}

            <ControlPanel
                cameraConnected={
                    cameraConnected
                }

                recording={
                    recording
                }

                onConnect={
                    handleConnect
                }

                onStart={
                    handleStartStreaming
                }

                onStop={
                    handleStopStreaming
                }

                
            />

        </div>

    );

}


export default Dashboard;