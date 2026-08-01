function Sidebar({
    cameraConnected,
    recorderStatus,
    gpsConnected,
    databaseConnected,
    savedFile,
}) {
    return (
        <div className="panel sidebar">

            <h2 className="section-title">
                SYSTEM STATUS
            </h2>

            <div className="status-box">

                <div className="status-item">
                    ● Camera: {cameraConnected ? "Connected" : "Disconnected"}
                </div>

                <div className="status-item">
                    ● Recorder: {recorderStatus}
                </div>

                <div className="status-item">
                    ● GPS: {gpsConnected ? "Connected" : "Disconnected"}
                </div>

                <div className="status-item">
                    ● Database: {databaseConnected ? "Online" : "Offline"}
                </div>

            </div>

            <div className="sidebar-footer">
                {savedFile}
            </div>

        </div>
    );
}

export default Sidebar;