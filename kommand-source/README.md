# Kommand Source

Kommand Source captures screens or windows on a schedule, keeps the
latest image for each channel in `debug/`, and publishes frames to Kommand
Central.

## Run

From the repository root:

```powershell
./setup_env.cmd
./run_source.cmd
```

The setup script downloads Python 3.12 through `uv`, creates the root `.venv`,
downloads all packages in `requirements.txt`, and verifies the application
imports. It is safe to run again to update dependencies. Use
`./setup_env.cmd -Recreate` to replace the environment completely.

`setup_env.cmd` is the recommended Windows entry point because it runs the
PowerShell setup without requiring a permanent execution-policy change. Any
arguments are passed through, so `./setup_env.cmd -Recreate` also works.

The setup process cannot activate `.venv` in the PowerShell window that launched
it. `run_source.cmd` therefore invokes `.venv\Scripts\python.exe` directly and
does not require activation. The equivalent manual command is:

```powershell
.\.venv\Scripts\python.exe .\kommand-source\run.py
```

On Linux, activate the virtual environment with `source .venv/bin/activate` and
run the same Python command.

Configuration is saved to `kommand-source/config.json`. The debug directory is
cleared whenever the application starts. The Central connection panel shows
outgoing H.264 payload and JSON metadata bandwidth across all channels as a
five-second rolling average in Mbps. It excludes WebSocket framing and network overhead.
Each channel row shows a 50-pixel preview of its latest diagnostic frame. The
preview is checked once per second; a capture error replaces it with a large X
until that channel captures successfully again.
Channels refresh their diagnostic WebP image at most once per second on a separate
bounded worker. Pending snapshots are replaced with the latest image; diagnostic
write failures are logged and reported without delaying or backing off video uploads.
Central can request an additional video keyframe when a browser viewer joins or
needs to resynchronize; this does not change the configured periodic interval.
Diagnostic images are saved atomically with unique temporary files. On Windows,
brief access or sharing locks from preview readers are retried before reporting
an error; a persistent failure leaves the previous image intact.

Source defaults `OPENBLAS_NUM_THREADS` to `1` before loading the window-capture
backend, avoiding an unused numerical thread pool. An existing setting is respected.

## Logs

Capture failures, upload errors, Central connection-check failures, and uncaught
application or worker exceptions are written to `kommand-source/logs/source.log`.
Entries include timestamps, thread names, channel names for capture/upload
errors, and exception tracebacks when available. The channel table and connection
indicator continue to show their existing error status.

Logs rotate at 5 MiB, retaining up to five older files (`source.log.1` through
`source.log.5`). Logs are retained when Source restarts; only diagnostic images
in `debug/` are cleared.

## Capture targets

- `screen`: use `0` for the combined desktop, or `1`, `2`, and so on for an
  individual monitor. An application window requires the `window` type rather
  than a window title as a screen target.
- `window` (Windows): select a visible, non-minimized window from the list,
  which refreshes when opened. **Refresh windows** opens the refreshed target
  list immediately without changing the selected window. Source captures its window handle and updates the
  displayed title every five seconds, so title changes do not interrupt capture.
  Selecting a window also saves its executable path and an editable **Title
  contains (reconnect match)** rule. Choose a stable, distinctive title fragment,
  for example `YouTube` rather than a video's full changing title. Use **Update**
  and **Save config** to persist the rule.
  After a reboot or application restart, Source reconnects only when exactly one
  visible, non-minimized window matches both the executable path and the
  case-insensitive title fragment. Missing or multiple matches are reported as
  errors and capture keeps retrying. A valid live binding stays attached through
  title changes; it does not switch to another matching window.
  Existing configurations gain a saved matching rule on Save/Start when their
  original window or a unique title match is available. If the old title has
  changed and the old process is gone, select the target again once, then save a
  stable title fragment. Existing channels and Central assignments are retained.
  Windows Graphics Capture renders the
  selected window even when other windows overlap it. Applications can block
  capture; capture errors are reported and retried. A valid entirely black scene
  is allowed and is not treated as a capture failure.

Existing `region` channels remain supported, but cannot be selected for new
channels in the UI.

Channels can be added, updated, disabled, or removed while capture is running.
On startup, the first channel in the displayed list is selected and its settings
are loaded into the editor. With no configured channels, the editor stays empty.
Choose **New** to clear the editor, then **Update** to add the channel. For an
existing channel, **Update** is enabled when its fields change. Invalid fields
are highlighted red. Changes apply when **Update** or **Remove** is clicked;
unchanged channels continue capturing. Source name and Central URL changes apply
on **Save**.
Previously published frames and Central panel assignments remain in place.

Linux window capture depends on the display system and is not yet enabled. Use a
screen target on Linux until X11 and Wayland requirements are decided.

## H.264 channels

Every channel delivers H.264 without audio. **Video FPS** is a target from 1 to
60 fps, defaulting to 15; actual throughput depends on capture, encoding and
network performance. Quality controls H.264 compression, and **Keyframe
(seconds)** controls the periodic refresh interval.
WebP, WebP + Patch and older image configurations migrate automatically to video.
Names, targets, quality, frame-rate settings and persistent window matching are
retained; Save/Start persists the migration. The old image capture interval is
retained as compatibility metadata but no longer controls delivery.

Identical captures do not encode another video packet; they send a heartbeat at
most once per second. Periodic keyframes and viewer refresh requests still work
while idle. Capture or dimension changes, upload failures and Central restarts
start a new stream with a keyframe. Window movement alone does not restart video.
Window capture remains necessary to obtain pixels from another application.

## Upload contract

One persistent WebSocket is opened per channel:

```text
/api/v1/sources/{source_name}/channels/{channel_name}/video-upload
```

Each packet has a JSON metadata message followed by binary Annex-B H.264 bytes.
Metadata includes protocol `1`, capture timestamp, dimensions, stream ID,
sequence, Central generation and a monotonic microsecond presentation timestamp.
Central acknowledges accepted packets and can request the next frame be a
keyframe. Idle heartbeats contain the accepted revision and generation without
binary data. Rejected packets or failed acknowledgements reset the connection
and encoder; there is no silent image/HTTP fallback. Update Source and Central together.

