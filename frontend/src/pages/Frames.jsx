import { useEffect, useState } from "react";
import FramesTable from "../components/FramesTable";
import { API_BASE } from "../api";
function Frames() {

    const [frames, setFrames] = useState([]);

    useEffect(() => {

        const fetchFrames = async () => {

            try {

                const response = await fetch(`${API_BASE}/frames`);
                console.log("Status:", response.status);
                const data = await response.json();
                console.table(
    data.slice(0, 10).map((f) => ({
        id: f.frame_id,
        name: f.image_name,
        path: f.image_path,
    }))
);

setFrames(data);

                setFrames(data);

            } catch (err) {

                console.error(err);

            }

        };

        fetchFrames();

    }, []);

    return (

        <div style={{ padding: "30px" }}>

            <h2>Captured Frames</h2>

            <FramesTable frames={frames} />
        </div>

    );

}

export default Frames;