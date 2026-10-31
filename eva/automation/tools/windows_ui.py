"""Optional Windows UI Automation adapter backed by pywinauto's UIA backend."""
from __future__ import annotations

import ctypes
import platform
import time
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Any

from eva.automation.engine.models import AutomationError, ExecutionContext, RiskLevel, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry

_MAX_WINDOWS = 250
_MAX_CONTROLS = 500
_MAX_DESCENDANTS_SCANNED = 5000
_CLICKABLE_TYPES = {"Button", "CheckBox", "RadioButton", "TabItem", "MenuItem", "Hyperlink", "ListItem", "ComboBox", "TreeItem"}


def _desktop():
    if platform.system().lower() != "windows":
        raise AutomationError("PLATFORM_UNSUPPORTED", "Windows UI Automation is available only on Windows.")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise AutomationError("DEPENDENCY_MISSING", "Windows UI Automation requires pywinauto. Install the Windows dependencies from requirements.txt.") from exc
    return Desktop(backend="uia")


def _window_matches(desktop: Any, title: str) -> list[Any]:
    if not isinstance(title, str) or not title.strip() or len(title) > 250:
        raise AutomationError("INVALID_ARGUMENT", "window_title must contain 1 to 250 characters.")
    query = title.strip().casefold()
    matches = []
    try:
        candidates = desktop.windows(visible_only=True, enabled_only=False)
    except Exception as exc:
        raise AutomationError("UI_ENUMERATION_FAILED", f"Could not enumerate Windows UI elements: {type(exc).__name__}: {exc}") from exc
    for window in candidates[:_MAX_WINDOWS]:
        try:
            title_text = window.window_text() or ""
            if query in title_text.casefold():
                matches.append(window)
        except Exception:
            continue
    if not matches:
        raise AutomationError("WINDOW_NOT_FOUND", f"No visible window title contains {title!r}.")
    if len(matches) > 1:
        choices = [str((win.window_text() or "")[:120]) for win in matches[:10]]
        raise AutomationError("AMBIGUOUS_WINDOW", f"Window title matched multiple windows: {choices}. Use a more specific title.")
    return matches


def _list_windows(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    desktop = _desktop()
    windows = []
    try:
        candidates = desktop.windows(visible_only=True, enabled_only=False)
    except Exception as exc:
        raise AutomationError("UI_ENUMERATION_FAILED", f"Could not enumerate windows: {type(exc).__name__}: {exc}") from exc
    truncated = len(candidates) > _MAX_WINDOWS
    for window in candidates[:_MAX_WINDOWS]:
        context.check_cancelled()
        try:
            name = (window.window_text() or "").strip()
            if not name:
                continue
            windows.append({
                "title": name[:250],
                "process_id": int(window.process_id()),
                "control_type": str(window.element_info.control_type or "Window"),
                "enabled": bool(window.is_enabled()),
            })
        except Exception:
            continue
    return ToolResult(True, "windows.list", {"windows": windows, "count": len(windows), "truncated": truncated}, "Visible Windows UI Automation windows listed.", verified=True)


def _focus_window(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    context.check_cancelled()
    window = _window_matches(_desktop(), args["window_title"])[0]
    try:
        window.set_focus()
        return ToolResult(True, "windows.focus", {"title": (window.window_text() or "")[:250]}, "Window focus requested.", verified=True)
    except Exception as exc:
        raise AutomationError("WINDOW_FOCUS_FAILED", f"Could not focus the selected window: {type(exc).__name__}: {exc}") from exc


def _read_controls(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    window = _window_matches(_desktop(), args["window_title"])[0]
    try:
        descendants = window.descendants()
    except Exception as exc:
        raise AutomationError("UI_ENUMERATION_FAILED", f"Could not inspect controls: {type(exc).__name__}: {exc}") from exc
    if len(descendants) > _MAX_DESCENDANTS_SCANNED:
        raise AutomationError("UI_TREE_TOO_LARGE", f"The window exposes more than {_MAX_DESCENDANTS_SCANNED} controls; narrow the target window.")
    controls = []
    for control in descendants:
        context.check_cancelled()
        try:
            info = control.element_info
            name = str(info.name or "").strip()
            control_type = str(info.control_type or "")
            if not name and not control_type:
                continue
            controls.append({
                "name": name[:200],
                "control_type": control_type[:80],
                "automation_id": str(info.automation_id or "")[:160],
                "enabled": bool(control.is_enabled()),
                "visible": bool(control.is_visible()),
            })
            if len(controls) >= args.get("limit", 100):
                break
        except Exception:
            continue
    return ToolResult(True, "windows.read_controls", {"window_title": (window.window_text() or "")[:250], "controls": controls, "count": len(controls), "truncated": len(controls) >= args.get("limit", 100)}, "Accessible control metadata read; control values and text contents are not collected.", verified=True)


def _screen_size() -> tuple[int, int]:
    if platform.system().lower() != "windows":
        raise AutomationError("PLATFORM_UNSUPPORTED", "Window arrangement is available only on Windows.")
    user32 = ctypes.windll.user32
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def _window_process_name(pid: int) -> str:
    """Return a process executable name without invoking a shell or reading command lines."""
    if platform.system().lower() != "windows":
        return ""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.c_ulong(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ""
        return PureWindowsPath(buffer.value).name.casefold()
    finally:
        kernel32.CloseHandle(handle)


def _move_window_handle(hwnd: int, x: int, y: int, width: int, height: int) -> dict[str, int]:
    """Restore and resize a real top-level window, returning its measured rectangle."""
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow(wintypes.HWND(int(hwnd)), 9)  # SW_RESTORE
    ok = user32.SetWindowPos(wintypes.HWND(int(hwnd)), wintypes.HWND(0), x, y, width, height,
                             0x0040 | 0x0004)  # SHOWWINDOW | NOZORDER
    if not ok:
        raise AutomationError("WINDOW_ARRANGE_FAILED", "Windows rejected the window-position request.")

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        raise AutomationError("WINDOW_VERIFY_FAILED", "Window was moved but its resulting position could not be verified.")
    return {"x": int(rect.left), "y": int(rect.top),
            "width": int(rect.right - rect.left), "height": int(rect.bottom - rect.top)}


def _select_unique_window(windows: list[Any], role: str, markers: tuple[str, ...], process_names: set[str]) -> Any:
    matches = []
    for window in windows:
        try:
            title = (window.window_text() or "").casefold()
            process_name = _window_process_name(int(window.process_id()))
            if any(marker in title for marker in markers) or process_name in process_names:
                matches.append(window)
        except Exception:
            continue
    if not matches:
        raise AutomationError("WINDOW_NOT_FOUND", f"No visible {role} window was found. Open it and retry.")
    if len(matches) > 1:
        if platform.system().lower() == "windows":
            get_foreground = ctypes.windll.user32.GetForegroundWindow
            get_foreground.restype = wintypes.HWND
            foreground = int(get_foreground() or 0)
        else:
            foreground = 0
        focused = [window for window in matches if int(getattr(window, "handle", -1)) == foreground]
        if len(focused) == 1:
            return focused[0]
        titles = [(window.window_text() or "")[:100] for window in matches[:8]]
        raise AutomationError("AMBIGUOUS_WINDOW", f"Multiple visible {role} windows matched {titles}; close extras or make one active, then retry.")
    return matches[0]


def _arrange_windows_side_by_side(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    if platform.system().lower() != "windows":
        raise AutomationError("PLATFORM_UNSUPPORTED", "Side-by-side window arrangement is supported only on Windows.")
    desktop = _desktop()
    browser = notepad = None
    deadline = time.monotonic() + 8.0
    while True:
        context.check_cancelled()
        try:
            candidates = desktop.windows(visible_only=True, enabled_only=False)
        except Exception as exc:
            raise AutomationError("UI_ENUMERATION_FAILED", f"Could not enumerate visible windows: {type(exc).__name__}: {exc}") from exc
        windows = []
        for window in candidates[:_MAX_WINDOWS]:
            try:
                if window.is_visible() and window.window_text() is not None:
                    windows.append(window)
            except Exception:
                continue
        try:
            browser = _select_unique_window(
                windows, "browser", ("google chrome", "microsoft edge", "mozilla firefox", "brave", "chromium", "vivaldi", "opera"),
                {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "chromium.exe", "vivaldi.exe", "opera.exe"},
            )
            notepad = _select_unique_window(windows, "Notepad", ("notepad",), {"notepad.exe"})
            break
        except AutomationError as exc:
            if exc.code != "WINDOW_NOT_FOUND" or time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
    if int(browser.handle) == int(notepad.handle):
        raise AutomationError("WINDOW_SELECTION_FAILED", "The browser and Notepad resolved to the same window handle.")
    width, height = _screen_size()
    if width < 640 or height < 480:
        raise AutomationError("SCREEN_TOO_SMALL", "The primary screen is too small to tile two usable windows.")
    left_width = width // 2
    right_width = width - left_width
    context.check_cancelled()
    browser_rect = _move_window_handle(int(browser.handle), 0, 0, left_width, height)
    context.check_cancelled()
    notepad_rect = _move_window_handle(int(notepad.handle), left_width, 0, right_width, height)
    expected_browser = {"x": 0, "y": 0, "width": left_width, "height": height}
    expected_notepad = {"x": left_width, "y": 0, "width": right_width, "height": height}
    verified = browser_rect == expected_browser and notepad_rect == expected_notepad
    if not verified:
        raise AutomationError("WINDOW_VERIFY_FAILED", "The windows moved but their final bounds did not match the requested side-by-side layout.")
    return ToolResult(True, "windows.arrange_side_by_side", {
        "browser_title": (browser.window_text() or "")[:250], "browser_bounds": browser_rect,
        "notepad_title": (notepad.window_text() or "")[:250], "notepad_bounds": notepad_rect,
        "screen_size": {"width": width, "height": height},
    }, "Browser and Notepad were placed in equal side-by-side screen regions and their final bounds were verified.", verified=True)


def _click_control(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    context.check_cancelled()
    window = _window_matches(_desktop(), args["window_title"])[0]
    mode = args.get("match_by", "name")
    query = args["selector"].strip()
    if not query:
        raise AutomationError("INVALID_ARGUMENT", "selector must not be empty.")
    try:
        descendants = window.descendants()
    except Exception as exc:
        raise AutomationError("UI_ENUMERATION_FAILED", f"Could not inspect controls: {type(exc).__name__}: {exc}") from exc
    if len(descendants) > _MAX_DESCENDANTS_SCANNED:
        raise AutomationError("UI_TREE_TOO_LARGE", f"The window exposes more than {_MAX_DESCENDANTS_SCANNED} controls; narrow the target window.")
    matches = []
    for control in descendants:
        context.check_cancelled()
        try:
            info = control.element_info
            candidate = str(info.name or "") if mode == "name" else str(info.automation_id or "")
            if candidate.casefold() == query.casefold() and str(info.control_type or "") in _CLICKABLE_TYPES and control.is_visible() and control.is_enabled():
                matches.append(control)
        except Exception:
            continue
    if not matches:
        raise AutomationError("CONTROL_NOT_FOUND", "No enabled, visible clickable control matched that selector.")
    if len(matches) > 1:
        raise AutomationError("AMBIGUOUS_CONTROL", f"The selector matched {len(matches)} controls; use a more specific automation id or control name.")
    try:
        matches[0].click_input()
        return ToolResult(True, "windows.click_control", {
            "window_title": (window.window_text() or "")[:250], "selector": query,
            "match_by": mode, "control_type": str(matches[0].element_info.control_type or ""),
        }, "The unique matching UI control was clicked. The application-specific result should still be checked.", verified=False)
    except Exception as exc:
        raise AutomationError("CONTROL_CLICK_FAILED", f"Could not click the selected control: {type(exc).__name__}: {exc}") from exc


def register_windows_ui_tools(registry: ToolRegistry) -> None:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    registry.register(ToolSpec("windows.list", "List visible top-level Windows UI Automation windows without reading their content.", empty,
        ("system.inspect",), "windows", timeout_seconds=10, aliases=("list windows",)), _list_windows)
    registry.register(ToolSpec("windows.focus", "Focus exactly one visible window identified by case-insensitive title substring.", {
        "type": "object", "properties": {"window_title": {"type": "string", "minLength": 1, "maxLength": 250}},
        "required": ["window_title"], "additionalProperties": False},
        ("desktop.input",), "windows", timeout_seconds=10, idempotent=False, aliases=("focus window",)), _focus_window)
    registry.register(ToolSpec("windows.read_controls", "Read bounded accessible control metadata from one uniquely matched window; does not collect control contents.", {
        "type": "object", "properties": {"window_title": {"type": "string", "minLength": 1, "maxLength": 250},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_CONTROLS}},
        "required": ["window_title"], "additionalProperties": False},
        ("system.inspect",), "windows", timeout_seconds=15, aliases=("inspect window controls",)), _read_controls)
    registry.register(ToolSpec("windows.arrange_side_by_side", "Place one visible browser window and one visible Notepad window side by side; verifies final window bounds.", empty,
        ("desktop.input",), "windows", timeout_seconds=15, idempotent=False, risk=RiskLevel.MODERATE,
        aliases=("arrange windows side by side", "tile browser and notepad")), _arrange_windows_side_by_side)
    registry.register(ToolSpec("windows.click_control", "Click exactly one enabled visible button-like UIA control; requires run-scoped approval.", {
        "type": "object", "properties": {"window_title": {"type": "string", "minLength": 1, "maxLength": 250},
        "selector": {"type": "string", "minLength": 1, "maxLength": 200},
        "match_by": {"type": "string", "enum": ["name", "automation_id"]}},
        "required": ["window_title", "selector"], "additionalProperties": False},
        ("desktop.input",), "windows", timeout_seconds=15, idempotent=False,
        risk=RiskLevel.HIGH, requires_approval=True, aliases=("click application control",)), _click_control)
