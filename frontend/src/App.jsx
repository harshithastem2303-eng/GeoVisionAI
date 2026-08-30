import { BrowserRouter, Routes, Route } from "react-router-dom";

import Dashboard from "./pages/Dashboard";
import Frames from "./pages/Frames";
import GarbageCollectors from "./pages/GarbageCollectors";
import CollectorProfile from "./pages/CollectorProfile";
import RFIDScanner from "./components/RFIDScanner";

function App() {
    return (
        <BrowserRouter>
            <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/frames" element={<Frames />} />
                <Route path="/garbage-collectors" element={<GarbageCollectors />} />
                <Route path="/garbage-collectors/:id" element={<CollectorProfile />} />
                <Route path="/rfid-scanner" element={<RFIDScanner />} />
            </Routes>
        </BrowserRouter>
    );
}

export default App;