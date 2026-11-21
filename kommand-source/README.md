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
outgoing request-body bandwidth across all channels as a five-second rolling
average in Mbps. It includes multipart upload overhead but excludes HTTP headers
and network protocol overhead.
Video channels refresh their diagnostic WebP image at most once per second.

Source defaults `OPENBLAS_NUM_THREADS` to `1` before loading the window-capture
backend, avoiding an unused numerical thread pool. An existing setting is respected.

## Capture targets

- `screen`: use `0` for the combined desktop, or `1`, `2`, and so on for an
  individual monitor. An application window requires the `window` type rather
  than a window title as a screen target.
- `window` (Windows): select a visible, non-minimized window from the list,
  which refreshes when opened. Source captures its window handle and updates the
  displayed title every five seconds, so title changes do not interrupt capture.
  If the window closes or its saved process identity no longer matches, select
  it again; Source does
  not silently switch to another window. Existing title-only settings are bound
  to a matching window when possible. Windows Graphics Capture renders the
  selected window even when other windows overlap it. Applications can block
  capture; this is reported as an error instead of writing an empty debug image.

Existing `region` channels remain supported, but cannot be selected for new
channels in the UI.

Channels can be added, updated, disabled, or removed while capture is running.
Choose **New** to clear the editor, then **Update** to add the channel. For an
existing channel, **Update** is enabled when its fields change. Invalid fields
are highlighted red. Changes apply when **Update** or **Remove** is clicked;
unchanged channels continue capturing. Source name and Central URL changes apply
on **Save**.
Previously published frames and Central panel assignments remain in place.

Linux window capture depends on the display system and is not yet enabled. Use a
screen target on Linux until X11 and Wayland requirements are decided.

## Channel formats

- **WEBP** sends a complete screenshot at each capture interval.
- **WEBP + PATCH** is the default, including for existing configurations. It sends
  full frames, smaller change patches, and heartbeats.
- **VIDEO** streams H.264 video frames without audio. Its separate **Video FPS**
  setting is a target from 1 to 60 fps, defaulting to 15; actual throughput
  depends on capture, encoding, and network performance.

Quality affects WebP compression and H.264 quality. The full-frame interval
controls periodic full frames in patch mode and keyframes in video mode.
The editor shows only the controls used by the selected format; switching
formats retains the other settings for later use.
Video restarts with a keyframe after a capture alignment or size change, an
upload error, or a Central restart. Updating formats while running restarts only
the affected channel.

## Upload contract

Each frame is sent as a multipart `POST` to:

```text
/api/v1/sources/{source_name}/channels/{channel_name}/frames
```

The image part is named `frame`, and metadata is sent in `X-Kommand-*` headers.
In patch mode, changed regions are sent as patches when smaller than a complete
frame; unchanged captures send a heartbeat. A full frame is sent on startup,
capture alignment or size changes, recovery after an upload error, and at the
configured full-frame interval (60 seconds by default), subject to capture and
network availability. If the full-frame interval is shorter than the capture
interval, patch mode captures at the full-frame interval. VIDEO sends ordered
H.264 packets to the channel's `/video` endpoint. Existing JPEG and PNG channel
settings are migrated to WebP + Patch when loaded. Update Central and Source
together: older Centrals cannot receive VIDEO, and older Sources using JPEG or
PNG are rejected.

