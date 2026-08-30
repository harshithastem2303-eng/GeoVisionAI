import { Card, CardContent, Typography, Divider } from "@mui/material";

function PropertyCard({ property }) {

    if (!property) {
        return (
            <Card sx={{ mt: 2 }}>
                <CardContent>
                    <Typography variant="h6">
                        Waiting for detection...
                    </Typography>
                </CardContent>
            </Card>
        );
    }

    return (
        <Card
            sx={{
                mt: 2,
                borderRadius: 3,
                boxShadow: 4,
            }}
        >
            <CardContent>

                <Typography variant="h5" gutterBottom>
                    Current Building
                </Typography>

                <Divider sx={{ mb: 2 }} />

                <Typography>
                    <strong>Frame ID:</strong>{" "}
                    {property.frame_id ?? "N/A"}
                </Typography>

                <Typography>
                    <strong>Owner:</strong>{" "}
                    {property.property?.owner_name ??
                     property.owner_name ??
                     "Unknown"}
                </Typography>

                <Typography>
                    <strong>Address:</strong>{" "}
                    {property.property?.address ??
                     property.address ??
                     "Unknown"}
                </Typography>

                <Typography>
                    <strong>Confidence:</strong>{" "}
                    {property.confidence != null
                        ? `${(property.confidence * 100).toFixed(1)}%`
                        : "N/A"}
                </Typography>
                <Typography>
                    <strong>Status:</strong>{" "}
                    {property.status ?? "Segregated"}
                </Typography>
                <Typography>
                    <strong>Latitude:</strong>{" "}
                    {property.latitude ?? "N/A"}
                </Typography>

                <Typography>
                    <strong>Longitude:</strong>{" "}
                    {property.longitude ?? "N/A"}
                </Typography>

                <Typography>
                    <strong>Altitude:</strong>{" "}
                    {property.altitude ?? "N/A"} m
                </Typography>

                <Typography>
                    <strong>Capture Time:</strong>{" "}
                    {property.capture_time ?? "N/A"}
                </Typography>

            </CardContent>
        </Card>
    );
}

export default PropertyCard;