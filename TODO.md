# Kommand TODO

This file is the working engineering backlog for the current Kommand vertical slice. Items are ordered roughly by risk and implementation value rather than by application.

## Security and ingestion

### Separate display and administration access

- Decide whether display viewing requires authentication.
- Protect layout administration independently from read-only display access.
- Do not expose future layout mutation APIs without administrative authorization.
- Document trusted-LAN, VPN, reverse-proxy, and public-internet deployment expectations.

### Bound Central memory use

- Limit the maximum number of known Sources and channels.
- Limit encoded bytes per frame and total encoded and decoded bytes held by the frame cache.
- Reject or evict channels according to an explicit policy.
- Rate-limit uploads per Source and channel.
- Do not let arbitrary new source/channel paths allocate memory indefinitely.
- Expose cache usage and rejection counts in Central diagnostics.
- Add stress tests for channel count, upload rate, and total memory bounds.

## Reliability and lifecycle

### Make Source worker shutdown reliable

- Source currently clears its worker registry after a five-second join even if a worker is still alive.
- Keep workers registered until they actually terminate.
- Prevent Start from creating a second worker for a channel that is still stopping.
- Align request timeouts with the shutdown deadline or make requests cancellable.
- Surface a `Stopping` or `Stop timed out` state in the UI.
- Ensure closing Source cannot leave duplicate capture activity running.
- Add tests for stop during capture, upload, retry backoff, and Windows Graphics Capture.

### Report Central server lifecycle accurately

- Do not show `Listening` immediately after starting the Uvicorn thread.
- Introduce explicit `Stopped`, `Starting`, `Running`, `Stopping`, and `Failed` states.
- Wait for a successful bind/startup signal before reporting `Running`.
- Surface invalid bind addresses, port conflicts, and unexpected server-thread exits.
- Keep Stop from blocking the Tkinter UI thread for up to five seconds.
- Disable Open Display until the server is confirmed running.
- Add tests for successful startup, port conflicts, and shutdown timeouts.

### Add channel freshness and connection state

- Calculate frame age from Central's receive time.
- Add configurable `stale` and `offline` thresholds.
- Expose `live`, `stale`, `offline`, and `never seen` states through the API.
- Preserve the last good frame while displaying its age and state.
- Show stale/offline overlays in the display without disturbing the layout.
- Show the same status in Central's channel table.
- Decide how and when abandoned channels are removed from the cache and assignment list.
- Test state transitions using a controllable clock.

### Improve Source capture/upload state reporting

- Track capture success and upload success independently.
- A failed upload should not make a successfully written debug frame appear to be a capture failure.
- Retain the most useful last error plus timestamps for last capture and last upload.
- Distinguish target-not-found, capture-blocked, encoding, debug-write, network, authentication, and server errors.
- Consider retaining capture cadence during upload failures rather than applying upload backoff to the entire capture loop.

## Code organization

### Split Central's API and ingestion

- Keep API route functions thin.
- Put upload validation and cache updates in an ingestion service.
- Keep layout and channel models independent of FastAPI and Tkinter.

### Introduce application controllers

- Add a `SourceController` for configuration, channel editing, runner lifecycle, and channel status.
- Add a `CentralController` for server lifecycle, channel summaries, and layout commands.
- Restrict Tkinter classes to widget construction, variable binding, and event forwarding.
- Move message formatting and validation results out of widget methods.
- Make controllers testable without creating a Tk root window.

### Finish common UI extraction

- Source still contains its obsolete local colour dictionary and `_configure_styles` implementation.
- Remove the duplicate theme code from Source.
- Keep `kommand-common` as the only source of desktop design tokens and shared controls.
- Break the current theme module into tokens, styling, and reusable widgets as it grows.
- Add common labelled fields, status badges, card toolbars, and validation/error presentation when repeated usage justifies them.
- Keep the local display synchronized with the shared semantic palette.

### Separate protocol contracts

- Create the planned `kommand-protocol` package.
- Define protocol version, header names, channel identifiers, error bodies, size limits, and supported formats in one place.
- Remove duplicated string literals from Source and Central.
- Define a compatibility policy before protocol version 2 is introduced.

## Layout and display

### Saved layouts

- Add named layouts rather than one global layout file.
- Support create, duplicate, rename, delete, and select operations.
- Decide whether layout selection is global or specific to each display window.
- Allow multiple local displays to show different saved layouts.
- Preserve assignments when a layout is temporarily inactive.
- Confirm before destructive root-grid replacement or removal of nested grids.

### Panel presentation settings

- Add a per-panel fit mode: `contain`, `cover`, or `stretch`.
- Default to `contain` to preserve the full source aspect ratio.
- Allow panel labels and metadata overlays to be shown or hidden.
- Add background colour and optional border settings.
- Consider a deliberate aspect-ratio setting for unassigned panels.
- Show a useful preview in Central while editing a layout.

### Layout model hardening

- Add an explicit layout revision.
- Validate maximum nesting depth and maximum total panel count.
- Recover gracefully from a corrupt or partially written layout file.
- Add layout schema versioning and migrations.
- Confirm operations that discard nested panels or assignments.
- Add tests for deep nesting, invalid paths, corrupt JSON, and persistence failures.

## Source capture improvements

### Stable window identity

- Window capture configuration currently persists a title, while the UI initially discovers an HWND.
- Titles can change and several windows can share the same title.
- Store additional identity such as executable name, process ID hints, window class, and last title.
- Re-resolve restarted applications without silently selecting the wrong window.
- Clearly report ambiguous or missing targets.
- Decide whether minimized-window capture is required.

### Linux capture support

- Decide the supported Linux desktop environments.
- Implement and test X11 screen, region, and window capture.
- Define the Wayland support boundary and required portal/user interaction.
- Hide unsupported capture types rather than allowing configurations that cannot run.
- Document required system packages in addition to Python dependencies.

### Debug output robustness

- Keep one atomic latest image per channel.
- Consider writing a small metadata sidecar containing capture time, target, dimensions, format, and last error.
- Report debug-folder cleanup/write failures in the channel UI.
- Confirm the debug folder path before recursive deletion.
- Define whether failure to write debug output is warning-only or fatal.

## Configuration and packaging

### Handle secrets safely

- Source currently writes its bearer token into ordinary `config.json`.
- Use Windows Credential Manager, a keyring, an environment variable, or a separate permission-restricted secret file.
- Never render existing tokens back as plain text.
- Add token replacement and removal flows.

### Configuration resilience

- Add explicit configuration schema versions.
- Preserve a last-known-good backup when writing configuration.
- Offer recovery or reset when configuration/layout JSON is corrupt.
- Validate capture targets according to their capture type before saving.
- Keep user data outside the source checkout for installed deployments.

### Dependency organization

- Continue using the requested root `requirements.txt` as the installation contract.
- Separate runtime and development/test dependencies within that constraint, for example through included requirements files.
- Pin or lock tested versions for reproducible deployment.
- Document non-Python dependencies and supported Python/Windows/Linux versions.
- Review the size and necessity of OpenCV, which is currently brought in by window capture.

## Testing and diagnostics

### Expand automated coverage

- Source retry/backoff and cancellation.
- Worker stop/start concurrency.
- Central cache limits and eviction.
- Authentication and authorization.
- Real image validation and decompression limits.
- Stale/offline channel transitions.
- Corrupt layout/config recovery.
- Uvicorn bind and startup failures.
- Local display updates and layout reconciliation.
- Real Windows Graphics Capture integration on a Windows test machine.
- Controller behaviour without Tkinter.

### Structured logging

- Add standard Python logging configuration to both applications.
- Include source, channel, operation, and error category as structured context.
- Keep logs bounded with rotation.
- Do not log tokens or screenshot bytes.
- Add an operator-accessible recent-event view.
- Ensure exceptions in background threads are never silently lost.

### Operational metrics

- Track frames captured, uploaded, accepted, rejected, and displayed.
- Track capture, encoding, upload, and end-to-display latency.
- Track current cache size and channel count.
- Track reconnects, stale channels, and dropped/replaced frames.
- Expose a small diagnostics endpoint or Central status panel.

## Recommended implementation order

1. Validate real image contents and bound Central memory/channel creation.
2. Fix Source worker shutdown and Central server lifecycle reporting.
3. Add stale/offline channel state.
4. Extract Central API modules and ingestion services.
5. Remove duplicated Source theme code and introduce application controllers.
6. Add named layouts, per-panel presentation settings, and update notifications.
7. Expand platform integration tests and operational diagnostics.
