import { API_BASE } from "../api";

function CameraPreview({ previewMessage, recording }) {
    return (
        <div className="panel preview">

            <h2 className="preview-title">
                LIVE CAMERA PREVIEW
            </h2>

            <div className="preview-screen">

                {recording ? (
                    <img
                        src={`${API_BASE}/video_feed?t=${Date.now()}`}
                        alt="Live Camera"
                        style={{
                            width: "100%",
                            height: "100%",
                            objectFit: "contain",
                            display: "block"
                        }}
                    />
                ) : (
                    <div className="preview-placeholder">
                        {previewMessage}
                    </div>
                )}

            </div>

            <div className="preview-footer">
                {previewMessage}
            </div>

        </div>
    );
}

export default CameraPreview;
