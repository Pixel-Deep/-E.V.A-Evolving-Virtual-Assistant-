# EVA automation additions (Windows-first, macOS-aware)

This update integrates the sample scripts in `add.zip` into EVA's existing
allow-listed automation bridge. It does not import the original `auto_brain.py`
directly because it starts threads, relies on external TTS/STT modules, and
uses fixed screen coordinates for Spotify. Those behaviors are brittle and
can cause false success reports.

## New and repaired capabilities

### Multi-action execution
- `open Spotify GitHub and YouTube`
- `open insta facebook and whatsap`
- `play Baby on YouTube and Spotify`
- `play Baby on YouTube and open GitHub`
- `open Google Drive, ChatGPT and Stack Overflow`
Each action receives an individual result. Partial failures are shown in the CLI instead of being collapsed into `Unknown error`.

### Website aliases
- Imports the 175 curated website aliases from the sample `web_data.py`.
- Supports common aliases/typos such as `insta`, `ig`, `fb`, `yt`, `whatsap`, `watsapp`, `drive`, `docs`, and `git hub`.
- Multiword sites are parsed longest-first (`Google Drive`, `Stack Overflow`, `YouTube Studio`).
- Known destinations are opened using HTTPS URLs.

### Browser controls
- Open/close tab
- Refresh/reload
- Switch next/previous tab
- Open browser history/bookmarks
- Go back/forward
- Open developer tools
- Toggle full screen
- Zoom in/out (supported key map)

### Scrolling and video-player shortcuts
- Scroll up/down, top/bottom
- Seek forward/backward 10 seconds
- Increase/decrease playback speed
- Next/previous video
These keyboard actions operate on the currently focused window/player; focus the correct browser tab first.

### System and file utilities
- Existing running-app listing remains available.
- Public IP lookup uses Python's standard library with a timeout.
- Create a new empty file of common types (`Python`, `Java`, `text`, `HTML`, `CSS`, `JavaScript`, `JSON`, `XML`, `CSV`, `Markdown`, `YAML`, `PDF`, `Word`, `Excel`, `PowerPoint`) in `Documents/EVA-created-files`.
- File creation never overwrites an existing file and strips path traversal from requested names.
- Existing local system info, network/Wi-Fi status, battery, screenshot, clipboard, typing, brightness, volume-key and lock-screen actions remain in the same bridge.

## Commands to try

```text
open insta facebook and whatsap
play baby on youtube and open github
open spotify github and youtube
open Canva
open new tab
close tab
refresh page
scroll down
scroll to top
seek forward 10 seconds
increase playback speed
find my public ip
create a Python file named hello
```

## Honest limitations
- Music commands open search results. They do not guarantee playback, because account state, autoplay policy, and user interaction vary by service.
- Keyboard shortcuts act on the foreground application and depend on browser/player focus.
- Public IP lookup requires internet access.
- File creation creates an empty file only; it does not generate program contents.
- The provided Spotify script used fixed screen coordinates; it was replaced by URL-based search because fixed-coordinate clicking breaks across resolutions and layouts.
- The provided speaker health script plays high-frequency tones/sweeps at substantial volume and imports a project-specific TTS module. It is not auto-run by voice/text commands; a safe diagnostic should require explicit consent, start at low volume, and include a stop option.
- Windows microphone/speaker capture tests still need a dedicated, permission-aware audio backend.
- Real OS operations require testing on the user's Windows machine. Automated tests mock desktop execution and do not prove that a website or shortcut physically worked.

## Validation
The automation tests cover task routing, aliases, multi-action parsing, partial failure reporting, browser shortcuts, and safe file creation. Run from the EVA root:

```powershell
python -m pytest -q
```
