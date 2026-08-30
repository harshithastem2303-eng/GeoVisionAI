import { DataGrid } from "@mui/x-data-grid";
import { Chip } from "@mui/material";
import { useState } from "react";
import ImageViewer from "./ImageViewer";
import { API_BASE } from "../api";

function FramesTable({ frames }) {
    const [selectedFrame, setSelectedFrame] = useState(null);
    const [viewerOpen, setViewerOpen] = useState(false);
    const columns = [
        {
            field: "frame_id",
            headerName: "ID",
            width: 90,
        },
        {
    field: "preview",
    headerName: "Preview",
    width: 120,
    sortable: false,
    renderCell: (params) => {

        const url = `${API_BASE}/${params.row.image_path.replace(/\\/g, "/")}`;

        return (
            <div
                style={{
                    width: 80,
                    height: 60,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    overflow: "hidden",
                    borderRadius: 6,
                    border: "1px solid #ccc",
                }}
            >
                <img
                    src={url}
                    alt={params.row.image_name}
                    style={{
                        width: "100%",
                        height: "100%",
                        objectFit: "cover",
                        display: "block",
                        cursor: "pointer",
                    }}
                    onClick={() => {
                        setSelectedFrame({
                            image: url,
                            data: params.row,
                        });

                        setViewerOpen(true);
                    }}
                />
            </div>
        );
    },
},

        {
        field: "image_name",
        headerName: "Filename",
        flex: 1,
        },

        {
            field: "capture_time",
            headerName: "Capture Time",
            flex: 1.5,
        },
        {
            field: "latitude",
            headerName: "Latitude",
            width: 130,
        },
        {
            field: "longitude",
            headerName: "Longitude",
            width: 130,
        },
        {
            field: "altitude",
            headerName: "Altitude",
            width: 110,
        },
        {
    field: "segregation_status",
    headerName: "Segregation Status",
    width: 180,
    renderCell: (params) => (
        <Chip
            label={params.value || "Segregated"}
            color={
                params.value === "Not Segregated"
                    ? "error"
                    : "success"
            }
            size="small"
        />
    ),
},
    ];

    return (
    <>
        <div style={{ height: 700, width: "100%" }}>
            <DataGrid
                rows={frames}
                columns={columns}
                rowHeight={70}
                getRowId={(row) => row.frame_id}
                pageSizeOptions={[10, 25, 50]}
                initialState={{
                    pagination: {
                        paginationModel: {
                            pageSize: 10,
                        },
                    },
                }}
            />
        </div>

        <ImageViewer
    open={viewerOpen}
    frame={selectedFrame}
    onClose={() => setViewerOpen(false)}
/>
    </>
);
}
export default FramesTable;