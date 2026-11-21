# Kommand Central

Kommand Central receives the latest frame from each Source channel and presents
all channels in a local Tkinter control-room display.

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
`central.log.5`). If the server cannot start, the status points to the log.

## Layouts

Central starts with a 2×2 root grid. Set the root rows and columns and select
**Apply root grid** to replace it. Select a leaf panel to assign a live
`source/channel`, or subdivide that panel into another rows×columns grid. Nested
grids can be repeated to create asymmetric layouts. Select a nested grid and use
**Remove nesting** to turn it back into a single unassigned panel.

The local display renders the recursive grid and preserves source aspect ratio
without cropping or stretching. Each panel shows its number and assigned
`source/channel` at the bottom, or `Unassigned` when no source is selected.
Nested panel numbers match the layout tree (for example, `Panel 1.2`).
Drag an assigned panel to another panel to move its channel into an empty slot
or swap the two assignments. The layout tree updates automatically and the
change is saved.
Use **Fullscreen** or F11 to toggle fullscreen,
and Escape to exit it. On Windows, fullscreen uses the monitor containing most
of the display window and restores its previous position and size on exit.
Layout configuration is saved atomically to
`kommand-central/layout.json`.

This first vertical slice keeps only the latest frame per channel in memory.
Frames disappear when Central restarts.

The Server panel shows inbound request-body bandwidth as a five-second rolling
average in Mbps. It includes incoming HTTP request bodies, including uploads,
and excludes network protocol overhead such as headers.

Uploads contain WebP frames and patches or ordered H.264 video packets. Decoded
dimensions must match the request headers. Frames and packets are limited to
25 MiB, 8192 pixels per dimension, and 33,554,432 total pixels. Patches are
applied only to the matching frame revision; video packets must follow their
stream sequence. Sources send a full frame or start a new video stream with a
keyframe to recover from a mismatch. Idle video decoders are released after
five minutes.
