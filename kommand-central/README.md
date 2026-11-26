# Kommand Central

Kommand Central receives the latest frame from each Source channel and presents
all channels in a control-room display with Tk controls and embedded native VLC video.

From the repository root:

```powershell
./setup_env.cmd
./run_central.cmd
```

The default server listens on `0.0.0.0:8000`. Use **Open display** on the
Central machine. Configure Source with Central's LAN address and port.

Server and application logs, including HTTP access requests, are written to
`kommand-central/logs/central.log` rather than the command line. Logs rotate at
5 MiB, retaining up to five older files (`central.log.1` through
`central.log.5`). If the server cannot start, check this log for details.

## Layouts

Drag the horizontal divider between **Display Layout** and **Live Channels** up
or down to resize the two sections. The divider is a thin line separated from
the panel borders by 12 pixels of window-background spacing on each side.
The preview resizes as you drag; both
sections retain a minimum height so they remain usable.

Display Layout has a tab for each output window. Use **+** to add an independent
layout, edit **Window name** to rename its tab and display, and use **Open display**
to open that tab's window. Tabs keep the same height when selected or hovered.
Other display windows stay open when changing tabs;
move each window to its monitor and use F11 for fullscreen. **Delete window** beside
**Remove nesting** asks for confirmation before removing the active tab and closing its display window.
Closing all tabs leaves **+** available to create a new one.

**Browser display** opens the selected layout in the default browser while the
Central server is running. It is an experimental, read-only display: edit the
layout, zoom, pan, and channel assignments in Central or the Tk display. The
browser reflects saved changes and offers a fullscreen button. Video channels
keep their existing canvas, decoder and connection when a panel is panned or
zoomed. Assignment changes restart only the affected panels; grid-structure
changes or a Central restart rebuild the layout. Video channels are relayed as
H.264 and decoded in the browser using WebCodecs (try a current
Chrome or Edge). Joining viewers, decoder recovery, and slow connections request
a fresh keyframe from Source rather than waiting for its periodic keyframe
interval. Slow viewers discard their backlog and resynchronize without closing
the connection. Restart both Central and Source to enable keyframe feedback.
The browser detects stalled connections, paused sources, and Central restarts.
A single initial preview image is loaded while a browser viewer waits for a
keyframe. Idle heartbeats keep static scenes live without unnecessary decoder
resets. New Source channels are always H.264; image endpoints remain compatible
with older clients during migration.

The desktop display embeds libVLC surfaces and consumes timestamped MPEG-TS at
each channel's `/video.ts` endpoint. Central remuxes the original H.264 packets
without re-encoding. VLC handles video decoding, GPU presentation, scaling and
frame timing; Tk only updates controls and surface geometry. Each visible slot
owns a native player, so repeated assignments currently decode independently.
Native feeds complete pending access units and send small, non-picture H.264
keepalives to advance the transport clock while a source is idle. Initial images
and isolated updates therefore display without waiting for another captured frame.
These keepalives do not re-encode images or add Source uploads.
Windows setup installs portable VLC in `.venv`; Linux requires system libVLC.
Missing runtime dependencies produce an explicit error, not a Pillow video fallback.
The existing fullscreen, panel numbering, saved zoom/pan and drag assignments
remain available. Embedded Tk/libVLC playback currently supports Windows and Linux.

Central still software-decodes incoming video to validate packet contents and
dimensions, but retains native decoded frames. Pillow conversion and diagnostic
WebP encoding are lazy and cached per revision, used only for editor/browser
snapshots, not the full-rate desktop playback path.

The window list is saved in `windows.json`. The original layout stays in
`layout.json`; additional layouts use `layout-<id>.json`. Removing a tab retains
its layout file for recovery, but removes it from the saved window list.

Central starts with a 2×2 root grid. Set the root rows and columns and select
**Apply root grid** to replace it. Select a leaf panel to assign a live
`source/channel`, or subdivide that panel into another rows×columns grid. Nested
grids can be repeated to create asymmetric layouts. Select a nested grid and use
**Remove nesting** to turn it back into a single unassigned panel.

The local display renders the recursive grid and initially fits each source
without cropping or stretching. Each panel shows its number and assigned
`source/channel` at the bottom, or `Unassigned` when no source is selected.
Nested panel numbers match the layout tree (for example, `Panel 1.2`).
Use the mouse wheel over a panel to zoom around the pointer, down to the
fit-to-panel scale; drag within the
panel to pan. A drag that leaves the original panel instead moves its channel
into an empty destination or swaps the two assignments. The in-progress pan is
discarded when a drag becomes a move. Zoom and pan belong to the panel, not the
assigned channel, and are saved with the layout; the layout tree updates
automatically after moves.
Use **Fullscreen** or F11 to toggle fullscreen,
and Escape to exit it. On Windows, fullscreen uses the monitor containing most
of the display window and restores its previous position and size on exit.
Layout configuration is saved atomically to
`kommand-central/layout.json`.
Each save uses a unique temporary file and briefly retries replacement when
Windows reports an access or sharing lock. Persistent failures are still
reported without overwriting the existing file.

This first vertical slice keeps only the latest frame per channel in memory.
Frames disappear when Central restarts.

The Server panel shows inbound request-body bandwidth as a five-second rolling
average in Mbps. It includes incoming video bytes and JSON metadata, plus legacy
HTTP request bodies, and excludes network protocol overhead such as headers.

New uploads contain ordered H.264 packets over persistent WebSockets. Legacy
HTTP image, patch and video ingestion remains available for older clients. Decoded
dimensions must match the request headers. Frames and packets are limited to
25 MiB, 8192 pixels per dimension, and 33,554,432 total pixels. Patches are
applied only to the matching frame revision; video packets must follow their
stream sequence. Sources start a new video stream with a keyframe to recover
from a mismatch. Video decoders without packets or valid heartbeats are released
after five minutes. Viewer queues are bounded and recover at keyframes.
