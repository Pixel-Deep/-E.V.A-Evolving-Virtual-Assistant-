# EVA Desktop Automation

This integration adds a deterministic desktop-automation route to EVA. The
language model classifies intent through `eva/core/task_router.py`; actions are
then mapped to an allow-listed operation in `eva/automation/desktop.py`.
The model is never allowed to submit arbitrary shell/PowerShell text.

## Supported actions

| Natural request | Action | Windows | macOS |
|---|---|---|---|
| `automation status` | Detect platform and report capabilities | Yes | Yes |
| `show battery` | Battery percentage/state when hardware exposes it | Yes | Yes |
| `show system info` | OS, CPU, RAM and host summary | Yes | Basic doctor output |
| `show network status` / `show IP address` | Local network interfaces, addresses and gateways | Yes | Basic network diagnostic |
| `show Wi-Fi status` | Wi-Fi SSID/state/signal where exposed | Yes | General diagnostics only |
| `increase brightness` / `set brightness to 60` | Adjust built-in display brightness when supported | WMI-compatible displays | Yes, when CLI supports it |
| `check audio devices` | Enumerate installed sound devices; no recording | Yes | Doctor diagnostics |
| `lock the computer` | Lock current workstation | Yes | Keyboard shortcut; permissions may block it |
| `open Downloads folder` / `open Documents folder` | Open common user folders | Yes | Depends on app/OS support |
| `list running apps` | List visible/running applications | Yes | Yes |
| `open Notepad` / `open Calculator` | Launch application | Yes | Yes |
| `quit Notepad` | Close matching process/app | Yes | Yes |
| `open YouTube` / `open example.com` | Open site or search phrase in browser | Yes | Yes |
| `increase volume`, `volume down`, `mute` | System media keys | Yes | Yes |
| `set volume to 40`, `volume level` | Exact volume level | Explicitly reports unavailable without an audio backend | Yes, if macOS CLI supports it |
| `play music`, `next song`, `previous song` | Media transport keys / native media controls | Yes | Yes |
| `take a screenshot` | Save screen capture | Yes | Yes |
| `read clipboard`, `copy ... to clipboard` | Clipboard read/write | Yes | Yes |
| `type hello world` | Type into the focused app | Yes | Yes |
| `press keys ctrl+t` | Allow-listed keyboard shortcut | Limited allow-list | macOS CLI key map |
| `speak hello`, `notify hello` | Text-to-speech / notification | Basic Windows speech/dialog | Yes |

## Platform notes

- The user's observed EVA environment is Windows. This package therefore has a
  Windows PowerShell backend using Windows APIs and standard Python modules.
- The provided PC-Automation project is macOS-only. Its CLI and website cache
  are bundled under `eva/automation/mac_pc/` and used on macOS.
- Windows exact volume-level control/read is intentionally reported as
  unavailable rather than falsely claiming success. Volume up/down/mute work
  via media keys. A future optional Core Audio backend can add exact levels.
- Screenshots, typing, clipboard, app launch/quit and key presses affect the
  real desktop. The user should keep the intended app focused and review the
  target before using them. `app quit` terminates the named process and may
  discard unsaved work; do not request it unless that is intended.
- Display brightness via WMI is hardware-dependent and commonly unavailable on external monitors/desktops.
- Audio-device listing is not the same as microphone capture or a speaker loopback test; those diagnostics are not exposed through EVA's common dispatcher.
- Network status reports local interfaces only. It does not query or reveal a public IP unless a separate explicit feature is added.
- Locking the workstation can interrupt active work, so invoke it only when intended.
- Hardware/permission-specific actions can fail. EVA reports the tool result;
  it does not claim that a blocked action succeeded.
- Unsupported operating systems and unsupported shortcuts return a structured
  error. The integration never falls back to executing arbitrary shell text.

## Try it in EVA

- `automation status`
- `show battery`
- `increase volume`
- `open YouTube`
- `take a screenshot`
- `list running apps`
- `read clipboard`
- `type hello world`
- `press keys ctrl+t`

## Files added/changed

- `eva/automation/desktop.py`: OS-aware dispatcher and safe action allow-list.
- `eva/automation/mac_pc/scripts/pc.py`: bundled macOS automation CLI.
- `eva/automation/mac_pc/assets/websites.json`: site alias cache.
- `eva/core/task_router.py`: deterministic desktop intent detection.
- `eva/core/orchestrator.py`: routes requests to the automation bridge and
  returns structured results.
- `tests/automation/test_desktop_automation.py`: routing and safety regression
  tests.

## Additional integrated automations

The extended dispatcher also recognizes:

- `show system info`: Windows host, OS, CPU, RAM and PowerShell summary.
- `show network status` / `show IP address`: local active interfaces, IPv4 addresses, gateways and DNS where available.
- `show Wi-Fi status`: Windows Wi-Fi SSID/state/signal when the WLAN interface exposes it; macOS falls back to system diagnostics.
- `increase brightness`, `brightness down`, `set brightness to 60`, `brightness level`: WMI-based Windows display control where hardware supports it; macOS uses the bundled brightness CLI.
- `check audio devices`: Windows sound-device enumeration or macOS diagnostics.
- `test microphone` / `test speakers`: delegates to the original macOS CLI's audio diagnostics. Windows explicitly reports that recording/loopback tests are not configured instead of claiming success.
- `lock the computer`: lock workstation on Windows; macOS uses the lock shortcut and may require Accessibility permission.
- `open Downloads folder`, `open Documents folder`, `open Desktop folder`, `open Pictures folder`, `open Music folder`, `open Videos folder`: open common Windows user folders.

These operations remain hardware-, permission- and OS-dependent. EVA does not claim unsupported actions worked. It does not execute arbitrary model-written shell commands, and destructive actions such as deleting files, installing software, purchasing items, shutting down, or restarting remain blocked pending a proper confirmation/policy flow.
