function CameraPreview({ previewMessage }) {
    return (
        <div className="panel preview">

            <h2 className="preview-title">
                LIVE CAMERA PREVIEW
            </h2>

            <div className="preview-screen">

                <div className="preview-placeholder">
                    {previewMessage}
                </div>

            </div>

            <div className="preview-footer">
                {previewMessage}
            </div>

        </div>
    );
}

export default CameraPreview;