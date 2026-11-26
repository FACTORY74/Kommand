"""Windows application-window discovery and bounds."""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import platform


class WindowDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    handle: int
    process_id: int
    process_start: int


def get_window_identity(handle: int) -> WindowIdentity:
    if platform.system() != "Windows":
        raise WindowDiscoveryError("Window discovery currently supports Windows only.")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id)):
        raise WindowDiscoveryError("The selected window is no longer available; select it again.")
    process = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not process:
        raise WindowDiscoveryError(
            f"Cannot identify the selected window's process (Windows error {ctypes.get_last_error()})."
        )
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            process, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        ):
            raise WindowDiscoveryError(
                f"Cannot identify the selected window's process (Windows error {ctypes.get_last_error()})."
            )
        started = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return WindowIdentity(handle, process_id.value, started)
    finally:
        kernel32.CloseHandle(process)


def list_visible_windows() -> list[WindowInfo]:
    if platform.system() != "Windows":
        raise WindowDiscoveryError("Window discovery currently supports Windows only.")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    windows: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        rect = wintypes.RECT()
        if not title or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width > 0 and height > 0:
            windows.append(WindowInfo(hwnd, title, rect.left, rect.top, width, height))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return sorted(windows, key=lambda item: item.title.casefold())


def get_window_executable(handle: int) -> str:
    identity = get_window_identity(handle)
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, identity.process_id)
    if not process:
        raise WindowDiscoveryError(f"Cannot read the window executable (Windows error {ctypes.get_last_error()}).")
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            raise WindowDiscoveryError(f"Cannot read the window executable (Windows error {ctypes.get_last_error()}).")
        if get_window_identity(handle) != identity:
            raise WindowDiscoveryError("The selected window changed while reading its executable.")
        return path.value
    finally:
        kernel32.CloseHandle(process)


def find_matching_window(executable: str, title_fragment: str) -> WindowInfo:
    query = title_fragment.strip().casefold()
    if not query or not executable.strip():
        raise WindowDiscoveryError("Window matching requires an executable and a title fragment.")
    expected = ntpath.normcase(ntpath.normpath(executable))
    candidates = [
        window for window in list_visible_windows()
        if query in window.title.casefold()
        and ntpath.normcase(ntpath.normpath(get_window_executable(window.handle))) == expected
    ]
    if not candidates:
        raise WindowDiscoveryError(
            f"Waiting for a visible, non-minimized {ntpath.basename(executable)} window containing '{title_fragment}'."
        )
    if len(candidates) != 1:
        raise WindowDiscoveryError(
            f"Multiple {ntpath.basename(executable)} windows contain '{title_fragment}'; use a more specific title fragment."
        )
    return candidates[0]


def resolve_matching_window(
    executable: str, title_fragment: str, identity: WindowIdentity | None = None,
) -> tuple[WindowInfo, WindowIdentity]:
    if identity is not None:
        try:
            current = get_window_identity(identity.handle)
        except WindowDiscoveryError:
            current = None
        if current == identity:
            actual = get_window_executable(identity.handle)
            if ntpath.normcase(ntpath.normpath(actual)) != ntpath.normcase(ntpath.normpath(executable)):
                raise WindowDiscoveryError("The bound window does not match the saved executable.")
            return resolve_window(identity), identity
    window = find_matching_window(executable, title_fragment)
    identity = get_window_identity(window.handle)
    window = resolve_window(identity)
    actual = get_window_executable(identity.handle)
    if (
        ntpath.normcase(ntpath.normpath(actual)) != ntpath.normcase(ntpath.normpath(executable))
        or title_fragment.strip().casefold() not in window.title.casefold()
        or get_window_identity(identity.handle) != identity
    ):
        raise WindowDiscoveryError("The matching window changed while reconnecting; retrying.")
    return window, identity


def find_window(title_query: str) -> WindowInfo:
    query = title_query.strip().casefold()
    if not query:
        raise WindowDiscoveryError("Select a window title.")
    matches = [item for item in list_visible_windows() if query in item.title.casefold()]
    exact = [item for item in matches if item.title.casefold() == query]
    candidates = exact or matches
    if not candidates:
        raise WindowDiscoveryError(f"No visible, non-minimized window matches '{title_query}'.")
    if len(candidates) > 1:
        raise WindowDiscoveryError("Window title is ambiguous; refresh and select its full title.")
    return candidates[0]


def _window_by_handle(handle: int) -> WindowInfo:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL

    if not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
        raise WindowDiscoveryError("The selected window is not visible or is minimized.")
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise WindowDiscoveryError("The selected window is no longer available; select it again.")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise WindowDiscoveryError("The selected window has no visible size.")
    return WindowInfo(handle, buffer.value.strip(), rect.left, rect.top, width, height)


def resolve_window(identity: WindowIdentity) -> WindowInfo:
    if get_window_identity(identity.handle) != identity:
        raise WindowDiscoveryError("The selected window has closed or changed; select it again.")
    window = _window_by_handle(identity.handle)
    if get_window_identity(identity.handle) != identity:
        raise WindowDiscoveryError("The selected window has closed or changed; select it again.")
    return window
