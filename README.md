# GeoVision AI Frontend

## Overview

The GeoVision AI Frontend is a React + Vite based web application developed for the GeoVision AI project. It provides a modern dashboard for monitoring camera status, GPS connectivity, database status, recording information, and live camera preview.

The frontend has been designed to closely replicate the original Python desktop dashboard while making it browser-based and ready for backend integration.

---

## Features

- Modern React-based Dashboard
- Live Camera Preview Panel
- System Status Monitoring
  - Camera Status
  - Recorder Status
  - GPS Status
  - Database Status
- Recording Statistics
  - FPS
  - Resolution
  - Recording Status
  - Duration
  - Frame Count
- Camera Control Panel
  - Connect Camera
  - Start Streaming
  - Stop Streaming
  - Capture Frame
  - Exit
- Responsive Dark Theme UI
- Modular Component Architecture
- Ready for Backend API Integration

---

## Tech Stack

- React 19
- Vite
- JavaScript (ES6)
- CSS3
- HTML5

---

## Folder Structure

```
frontend
│
├── public
│
├── src
│   ├── assets
│   │
│   ├── components
│   │   ├── Header.jsx
│   │   ├── Sidebar.jsx
│   │   ├── CameraPreview.jsx
│   │   ├── StatusCards.jsx
│   │   └── ControlPanel.jsx
│   │
│   ├── pages
│   │   └── Dashboard.jsx
│   │
│   ├── App.jsx
│   ├── main.jsx
│   └── index.css
│
├── package.json
├── vite.config.js
└── README.md
```

---

## Installation

Clone the repository

```bash
git clone <repository-url>
```

Navigate to the frontend directory

```bash
cd frontend
```

Install dependencies

```bash
npm install
```

Start the development server

```bash
npm run dev
```

Open your browser

```
http://localhost:5173
```

---

## Current Functionality

### Dashboard

Displays the complete GeoVision AI monitoring interface.

### Connect Camera

Currently simulates camera connection.

Updates:

- Camera Status
- Database Status
- Preview Message

### Start Streaming

Currently simulates streaming.

Updates:

- Recorder Status
- Preview Status Message

### Stop Streaming

Stops the simulated recording session.

### Capture Frame

Placeholder button for future backend integration.

### Exit

Placeholder for application exit functionality.

---

## Components

### Header

Displays the application title and subtitle.

### Sidebar

Displays:

- Camera Status
- Recorder Status
- GPS Status
- Database Status
- Latest Recording

### CameraPreview

Reserved area for displaying the live video feed from the backend.

Currently shows placeholder messages until video streaming is integrated.

### StatusCards

Displays

- FPS
- Resolution
- Recording Status
- Duration
- Frames

### ControlPanel

Contains dashboard control buttons.

---

## Future Backend Integration

The frontend has been structured to integrate seamlessly with the backend.

Future enhancements include:

- Live camera stream
- GPS data updates
- Database connectivity
- Real-time recording status
- Frame capture
- Recording download
- WebSocket support
- REST API integration

---

## Development Notes

This frontend is intentionally separated from the backend to allow independent development.

All current button actions simulate expected system behavior and will later be connected to backend APIs.

---

## Author

Developed as part of the **GeoVision AI** project.

Frontend developed using **React + Vite**.
