import {
    Dialog,
    DialogContent,
    IconButton,
    Button,
} from "@mui/material";

import CloseIcon from "@mui/icons-material/Close";

function ImageViewer({ open, frame, onClose }) {
    if (!frame) return null;

    const { image, data } = frame;
    return (
        <Dialog
            open={open}
            onClose={onClose}
            maxWidth="lg"
            fullWidth
        >
            <IconButton
                onClick={onClose}
                sx={{
                    position: "absolute",
                    right: 10,
                    top: 10,
                    zIndex: 10,
                    backgroundColor: "white",
                }}
            >
                <CloseIcon />
            </IconButton>

            <DialogContent
    sx={{
        background: "#111",
        color: "white",
    }}
>
    <div
        style={{
            display: "flex",
            justifyContent: "center",
        }}
    >
        <img
            src={image}
            alt="Frame"
            style={{
                maxWidth: "100%",
                maxHeight: "60vh",
                objectFit: "contain",
            }}
        />
    </div>

    <div style={{ marginTop: 20 }}>

        <h3>Frame Information</h3>

        <p><strong>Frame ID:</strong> {data.frame_id}</p>

        <p><strong>Capture Time:</strong> {data.capture_time}</p>

        <p><strong>Latitude:</strong> {data.latitude}</p>

        <p><strong>Longitude:</strong> {data.longitude}</p>

        <p><strong>Altitude:</strong> {data.altitude} m</p>

        <p>
            <strong>Status:</strong>{" "}
            {data.processed ? "Processed" : "Pending"}
        </p>

        <Button
            variant="contained"
            sx={{ mt: 2 }}
            onClick={() =>
                window.open(
                    `https://www.google.com/maps?q=${data.latitude},${data.longitude}`,
                    "_blank"
                )
            }
        >
            Open in Google Maps
        </Button>

    </div>
</DialogContent>

        </Dialog>
    );
}

export default ImageViewer;