import { useState } from "react";

import Header from "../components/Header";
import Sidebar from "../components/Sidebar";
import CameraPreview from "../components/CameraPreview";
import StatusCards from "../components/StatusCards";
import ControlPanel from "../components/ControlPanel";

function Dashboard() {
    // ==============================
    // Dashboard State
    // ==============================

    const [cameraConnected, setCameraConnected] = useState(false);

    const [recorderStatus, setRecorderStatus] = useState("Idle");

    const [gpsConnected, setGpsConnected] = useState(false);

    const [databaseConnected, setDatabaseConnected] = useState(false);

    const [fps, setFps] = useState(0);

    const [resolution, setResolution] = useState("640 × 480");

    const [recording, setRecording] = useState(false);

    const [duration, setDuration] = useState("00:00:00");

    const [frames, setFrames] = useState(0);

    const [savedFile, setSavedFile] = useState("No recording saved yet");

    const [previewMessage, setPreviewMessage] = useState(
        "Camera is currently inactive"
    );

    // ==============================
    // Temporary Button Functions
    // (Replace with backend API calls later)
    // ==============================

    const handleConnect = () => {
        setCameraConnected(true);
        setDatabaseConnected(true);

        setPreviewMessage(
            "Press Start Streaming to begin preview and recording"
        );
    };

    const handleStartStreaming = () => {
        setRecording(true);
        setRecorderStatus("Recording");

        setPreviewMessage("Streaming has started...");
    };

    const handleStopStreaming = () => {
        setRecording(false);
        setRecorderStatus("Idle");

        setPreviewMessage("Stream stopped");
    };

    const handleCapture = () => {
        alert("Frame Captured");
    };

    // ==============================
    // UI
    // ==============================

    return (
        <div className="dashboard">

            <Header />

            <div className="content">

                <Sidebar
                    cameraConnected={cameraConnected}
                    recorderStatus={recorderStatus}
                    gpsConnected={gpsConnected}
                    databaseConnected={databaseConnected}
                    savedFile={savedFile}
                />

                <CameraPreview
                    previewMessage={previewMessage}
                />

            </div>

            <StatusCards
                fps={fps}
                resolution={resolution}
                recording={recording}
                duration={duration}
                frames={frames}
            />
    
            <ControlPanel
                cameraConnected={cameraConnected}
                recording={recording}
                onConnect={handleConnect}
                onStart={handleStartStreaming}
                onStop={handleStopStreaming}
                onCapture={handleCapture}
            />

        </div>
    );
}

export default Dashboard;