function ControlPanel({
    cameraConnected,
    recording,
    onConnect,
    onStart,
    onStop,
    onCapture,
}) {
    return (
        <div className="panel controls">

            <button
                className="btn connect-btn"
                onClick={onConnect}
            >
                Connect Camera
            </button>

            <button
                className="btn start-btn"
                disabled={!cameraConnected}
                onClick={onStart}
            >
                {recording ? "Streaming..." : "Start Streaming"}
            </button>

            <button
                className="btn stop-btn"
                disabled={!recording}
                onClick={onStop}
            >
                Stop Streaming
            </button>

            <button
                className="btn capture-btn"
                disabled={!recording}
                onClick={onCapture}
            >
                Capture Frame
            </button>

            <div className="spacer"></div>

            <button
                className="btn exit-btn"
            >
                Exit
            </button>

        </div>
    );
}

export default ControlPanel;