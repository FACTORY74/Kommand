# Kommand Architecture

## Current video pipeline

Channels now deliver H.264 exclusively. Source uses a persistent WebSocket per
channel, sends idle heartbeats instead of redundant video, and saves diagnostic
snapshots on a separate bounded worker. Existing image/patch configurations
migrate without changing channel identities.

Central validates encoded packets and retains decoded native frames; Pillow
snapshots are lazy. Desktop output retains Tk controls and embeds libVLC players
fed timestamped MPEG-TS remuxed from the original H.264 packets. Browser output
uses WebCodecs. Neither full-rate video output path uses Tk PhotoImage rendering.
Window/screen capture is still required on Source; direct originating media
streams are not implemented. The sections below include historical proposals.

## 1. Purpose

Kommand is a control-room display system. Lightweight agents capture screenshots from participating Windows and Linux machines and publish them as named channels to a central server. The central server maintains the available channels and presents them in configurable layouts through a local, full-screen-capable Tkinter display.

The initial design favours Python and source distribution over platform-specific compiled binaries.

## 2. Design goals

- Run screenshot sources on Windows and Linux without maintaining native application binaries.
- Capture either an entire screen or a selected application window on a configurable schedule.
- Publish each capture under a stable, human-readable channel name.
- Allow one source process to expose one or more channels.
- Let a central operator arrange channels into reusable control-room layouts.
- Present layouts on monitors attached to Central and support a clean full-screen display mode.
- Keep installation, configuration, and operation understandable for a small deployment.
- Make intermittent sources and network failures visible without destabilising the display.

## 3. Applications

### 3.1 Kommand Source

`Kommand-Source` is the capture agent installed on a contributing machine.

Responsibilities:

- Provide a local UI for configuring the central server connection.
- Discover available screens and, where the operating system permits, capturable windows.
- Configure one or more named channels.
- Associate each channel with a screen, window, or region and a capture frequency.
- Capture, encode, and transmit images to `Kommand-Central`.
- Keep the latest captured image for each channel in a local `debug` folder.
- Display connection state, last successful capture, last upload, and errors.
- Persist local configuration across restarts.
- Retry temporary failures with bounded backoff.

The capture pipeline should be separated from the UI so that the same agent can later run unattended or as a background service. Operating-system-specific capture and window-discovery code should sit behind a common Python interface.

An initial channel model is:

| Field | Purpose |
| --- | --- |
| `name` | Stable operator-facing channel name, unique within a Central instance |
| `capture_type` | Screen, window, or region |
| `capture_target` | Platform-specific selector for the chosen target |
| `interval_ms` | Time between capture attempts |
| `full_frame_interval_seconds` | Maximum interval between complete WebP frames |
| `quality` | Encoding quality or compression setting |
| `enabled` | Whether this channel is currently publishing |

Window identity is inherently less stable than screen identity: titles change, processes restart, and several windows may match. The Source UI should store enough information to re-identify a target and clearly report when the configured window cannot be found.

### 3.2 Kommand Central

`Kommand-Central` is the hub, API server, and local display host.

Responsibilities:

- Authenticate or otherwise identify Source instances.
- Receive and validate channel frames and metadata.
- Maintain the latest frame and health state for every channel.
- Expose channel state and images to the local display.
- Create, edit, persist, and select layouts.
- Serve an operator view for configuration and a distraction-free display view.
- Mark stale or disconnected channels visibly.
- Enforce upload limits and avoid unbounded image retention in memory or on disk.

Central should initially be a single deployable Python service. Its internal boundaries should still distinguish ingestion, channel state, layout persistence, and web delivery so these concerns can be split later if scale requires it.

### 3.3 Kommand Display

The display is a Tkinter window running on the `Kommand-Central` machine.

Responsibilities:

- Render a selected layout responsively at control-room resolutions.
- Display the newest image for each assigned channel.
- Indicate stale, unavailable, and never-seen channels without collapsing the layout.
- Support full-screen operation and a minimal kiosk-style presentation.
- Reflect new local channel frames and layout changes.

The display reads the in-process channel and layout stores.

## 4. Proposed repository shape

Each deployable application has its own subfolder, with shared protocol definitions kept explicit and small.

```text
Kommand/
├── architecture.md
├── README.md
├── requirements.txt              # All Python runtime and development dependencies
├── docs/
│   ├── protocol.md               # Wire protocol once agreed
│   └── deployment.md
├── kommand-common/
│   └── src/kommand_common/
│       └── ui/                    # Shared design tokens and desktop controls
├── kommand-source/
│   ├── README.md
│   ├── src/kommand_source/
│   │   ├── capture/               # Common interface and platform implementations
│   │   ├── transport/             # Central client and retry behaviour
│   │   ├── ui/                    # Local configuration/status UI
│   │   ├── config.py
│   │   └── main.py
│   └── tests/
├── kommand-central/
│   ├── README.md
│   ├── src/kommand_central/
│   │   ├── api/                   # Source ingestion APIs
│   │   ├── channels/              # Latest-frame and health state
│   │   ├── layouts/               # Layout model and persistence
│   │   ├── desktop/               # Display and administrative UI
│   │   ├── config.py
│   │   └── main.py
│   └── tests/
├── kommand-protocol/
│   ├── src/kommand_protocol/      # Shared schemas and protocol versioning
│   └── tests/
└── integration-tests/
    ├── fixtures/
    └── tests/
```

`kommand-protocol` should contain data contracts only, with few dependencies. Capture and server implementation code should not leak into it. If sharing this package makes installation harder during early development, the same schemas can temporarily be duplicated or generated from a protocol specification, but the wire contract must remain versioned.

All Python dependencies are declared in the repository-level `requirements.txt`. The individual application folders do not maintain separate dependency files. Platform-specific dependencies may use environment markers in this file so that Windows-only packages are not installed on Linux and vice versa.

## 5. Data flow

1. A Source starts and loads its local configuration.
2. It clears and recreates its local `debug` folder so files from a previous run cannot be mistaken for current output.
3. It connects to Central and identifies itself and its configured channels.
4. Each enabled channel captures its target according to its own interval.
5. The captured image is encoded and saved as that channel's latest image in the `debug` folder, replacing the previous copy.
6. Source sends a full WebP frame or a smaller WebP patch referencing the acknowledged frame revision. Unchanged captures send a heartbeat.
7. Central validates the update and atomically replaces or patches that channel's latest frame.
8. The local display detects new revisions and refreshes the assigned panels.
9. A full frame is sent periodically, on alignment changes, or after an upload error.
10. If updates cease, Central and the display retain the last frame but mark it stale after a configured threshold.

The capture interval and full-frame interval are configurable per channel.

## 6. Communication model

The precise transport remains to be selected. A pragmatic initial design is:

- HTTP(S) endpoints for source registration, status, configuration-independent metadata, and frame upload.
- In-process channel and layout state for the local Tkinter display.
- JSON for control metadata and a binary request body or multipart upload for image data.
- An explicit protocol version on Source-to-Central requests.

This keeps Source clients straightforward and avoids holding a long-lived connection per captured channel. A persistent Source WebSocket can be considered later if Central needs to send commands or configuration to agents.

Central should apply at least these limits:

- Maximum encoded frame size.
- Maximum image dimensions.
- Allowed image formats.
- Maximum update frequency per channel/source.
- Request and idle timeouts.
- A bounded number of known channels.

## 7. State and persistence

Source configuration is local to each agent and should initially use a human-readable file, with secrets stored separately where feasible.

For capture diagnostics, Source owns a local `debug` folder. On every startup it removes the folder's existing contents before beginning capture. After each successful capture, Source writes the encoded image to a deterministic filename derived safely from the channel name, replacing that channel's previous file. Consequently, the folder contains at most one current image per channel and is diagnostic output rather than screenshot history. Writes should use a temporary file followed by an atomic replace so an operator never opens a partially written image. Failure to prepare or write the debug folder should be logged clearly; whether it prevents capture from starting should be configurable, with non-fatal behaviour as the default.

Central persists:

- Known source identities and credentials, if authentication is enabled.
- Channel metadata and operator-friendly names.
- Layout definitions and the currently selected layout, if there is a global selection.
- Server configuration and stale-channel thresholds.

Central does not need to persist screenshot history in the first version. It should keep only the latest frame for each channel, preferably in memory, with optional temporary disk storage if memory pressure or multi-process serving requires it. Recording and historical review are separate future capabilities with different storage and privacy implications.

A lightweight database such as SQLite is suitable for the initial metadata and layout store. The image cache should remain outside relational storage.

## 8. Layout model

A layout is a named collection of positioned tiles. Each tile references a channel and includes presentation settings such as:

- Position and size within a grid.
- Fit mode: contain, cover, or stretch.
- Optional label and status overlay.
- Background colour.
- Layer/order if overlapping layouts are supported.

The first editor can use a fixed grid, which is easier to persist and render consistently across screen sizes than arbitrary pixel coordinates. Layout definitions should use logical grid units so a layout scales to the display window.

## 9. Reliability and failure behaviour

- A failed capture affects only its channel; other channels continue publishing.
- A failed upload is retried with exponential backoff and jitter, without building an unbounded queue of obsolete screenshots.
- When a new frame is ready, an older unsent frame may be discarded: current visibility is more valuable than screenshot history.
- Central keeps the last good frame while showing its age and stale status.
- The local display reads the latest state directly from Central.
- Source and Central logs include source identity, channel, timestamp, and actionable error details.
- Wall-clock timestamps should use UTC on the wire; elapsed-time decisions should use monotonic clocks where possible.

## 10. Security and privacy

Screenshots may contain credentials, personal data, or operationally sensitive material. Even a trusted-network deployment should account for this.

- Support TLS directly or document operation behind a TLS reverse proxy.
- Give each Source its own revocable credential rather than sharing one global password.
- Authorise channel publication so one Source cannot overwrite another Source's channels accidentally.
- Avoid logging image bodies or secrets.
- Bind administrative and ingestion interfaces deliberately; do not assume every network interface is safe.
- Make capture state obvious in the Source UI.
- Do not retain screenshot history unless the operator explicitly enables a future recording feature.

Browser viewing and layout administration may need separate permissions even if the first trusted-network release uses a simpler access model.

## 11. Packaging and operation

- Target a documented, currently supported Python version shared by Source and Central.
- Use isolated virtual environments and install all dependencies from the repository-level `requirements.txt`.
- Keep platform-specific dependencies in that file behind Python environment markers.
- Provide Python launcher modules for Source and Central; command wrappers can be added later if useful.
- Central should be straightforward to run directly for development and behind a reverse proxy for deployment.
- Source should initially run as an interactive application; unattended startup and OS service integration can follow without changing its capture core.

Python-first avoids maintaining our own compiled executables, but some screenshot libraries may still depend on native OS APIs or binary Python wheels. Dependency selection and installation testing must therefore cover the supported Windows and Linux versions.

## 12. Testing strategy

- Unit-test layout, configuration, channel-state, staleness, and retry logic independently of real screens.
- Define a fake capture provider that generates deterministic images for development and automated tests.
- Contract-test Source uploads against Central using the shared protocol schemas.
- Run platform-specific capture tests on both Windows and Linux.
- Browser-test layout rendering, reconnection, full-screen presentation, and stale states.
- Include an integration scenario with multiple simulated Sources publishing at different rates.

## 13. Deferred capabilities

The architecture should leave room for these features without treating them as first-release requirements:

- Central-controlled Source configuration.
- Screenshot history, recording, and playback.
- Video or delta encoding for high-frequency channels.
- Multiple Central nodes and shared image storage.
- Layout rotation or scheduled layout selection.
- Alerts based on missing or stale channels.
- Redaction regions applied before a screenshot leaves the Source machine.
- Remote pointer/keyboard control; this would require a separate security design and is not implied by screenshot capture.

## 14. Open questions

The following decisions should be made before implementation is scaffolded:

1. What capture rates are expected: occasional monitoring (for example every 5–30 seconds), near-live updates (1–2 frames per second), or both?
2. Should one Source process publish multiple screens/windows, or should each configured capture run as a separate Source instance?
3. Does “capture a window” mean the window must be visible on screen, or must Kommand capture obscured and minimised windows where the OS permits it?
4. Which Windows and Linux versions and Linux display systems (X11, Wayland, or both) must the first release support?
5. Is Kommand intended only for a trusted LAN/VPN, or must Central be safe to expose through the public internet?
6. Should Source channel names be globally unique, or should identity be hierarchical, such as `source-name/channel-name`?
7. Who edits layouts, and is a single shared layout sufficient initially?
8. Is a desktop Source UI required, or would a local browser UI served by the Source process be acceptable and preferable for a Python-first implementation?
9. Should Central only receive images, or should it also remotely control Source settings and capture schedules?
10. What is the expected maximum number of Sources/channels, typical resolution, and available network bandwidth?
11. Should the first release include authentication for Sources, viewers, and administrators, or is staged security acceptable on an isolated network?
12. Are Docker deployment for Central and automatic startup/service installation for Source part of the first milestone?

## 15. Initial implementation boundary

A useful first vertical slice is one Source publishing one full-screen channel to one Central instance, with a local display showing the latest frame and a visible stale state. The next slice adds multiple channels and a persisted fixed-grid layout. Window selection, layout editing, authentication, and service installation can then be added against working end-to-end contracts.
