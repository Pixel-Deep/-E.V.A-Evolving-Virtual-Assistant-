"""Cross-platform desktop automation bridge for EVA.

Windows uses PowerShell and Python standard library. macOS delegates to the
bundled PC-Automation CLI. Commands are deliberately allow-listed; arbitrary
shell commands are never accepted from the model.
"""
from __future__ import annotations

import base64
import ast
import ctypes
import json
import math
import operator
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote, quote_plus

AUTOMATION_DIR = Path(__file__).resolve().parent
MAC_PC = AUTOMATION_DIR / "mac_pc" / "scripts" / "pc.py"


def _kill_process_tree(pid: int) -> None:
    """Terminate a process and all its child processes on Windows."""
    if platform.system().lower() == "windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass


def _ps(script: str, timeout: int = 15, context: Any = None):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    proc = None
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if context is not None and hasattr(context, "register_process"):
            context.register_process(proc)
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc is not None:
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except Exception:
                pass
        return {"ok": False, "error": f"PowerShell command timed out after {timeout} seconds.", "timeout": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if proc is not None and context is not None and hasattr(context, "unregister_process"):
            context.unregister_process(proc)
    stdout = stdout.strip()
    if proc.returncode:
        return {"ok": False, "error": stderr.strip() or stdout or "PowerShell command failed"}
    try:
        parsed = json.loads(stdout) if stdout else {}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", True)
            return parsed
        return {"ok": True, "result": parsed}
    except json.JSONDecodeError:
        return {"ok": True, "result": stdout}


def _windows(action: str, value: str = "", *, target: str | None = None, context: Any = None):
    # PowerShell literals are single-quoted, with embedded apostrophes doubled.
    v = "'" + value.replace("'", "''") + "'"
    if action == "doctor":
        script = r"""
$os = [System.Environment]::OSVersion.VersionString
$vol = (Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Name)
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
[pscustomobject]@{ok=$true; platform=$os; python=$PSVersionTable.PSVersion.ToString(); sound_device=$vol; battery_available=[bool]$bat; capabilities=@('apps','web','battery','clipboard','screenshot','volume keys','browser/media keys','text typing') } | ConvertTo-Json -Compress
"""
    elif action == "system_info":
        script = r"""
$os=Get-CimInstance Win32_OperatingSystem
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name
$mem=Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{ok=$true;computer=$env:COMPUTERNAME;user=$env:USERNAME;os=$os.Caption;os_version=$os.Version;cpu=$cpu;ram_gb=[math]::Round($mem.TotalPhysicalMemory/1GB,2);powershell=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
"""
    elif action == "network_info":
        script = r"""
$cfg=Get-NetIPConfiguration | Where-Object {$_.NetAdapter.Status -eq 'Up'} | ForEach-Object {[pscustomobject]@{adapter=$_.InterfaceAlias;ipv4=@($_.IPv4Address.IPAddress);gateway=@($_.IPv4DefaultGateway.NextHop);dns=@($_.DNSServer.ServerAddresses)}}
[pscustomobject]@{ok=$true;interfaces=@($cfg)}|ConvertTo-Json -Depth 5 -Compress
"""
    elif action == "wifi_status":
        script = r"""
$raw=netsh wlan show interfaces 2>$null
if($LASTEXITCODE -ne 0 -or -not $raw){[pscustomobject]@{ok=$false;available=$false;error='Wi-Fi interface status is unavailable.'}|ConvertTo-Json -Compress}
else{$ssid=($raw | Select-String '^\s*SSID\s*:\s*(.+)$' | Select-Object -First 1).Matches.Groups[1].Value.Trim();$state=($raw | Select-String '^\s*State\s*:\s*(.+)$' | Select-Object -First 1).Matches.Groups[1].Value.Trim();$signal=($raw | Select-String '^\s*Signal\s*:\s*(.+)$' | Select-Object -First 1).Matches.Groups[1].Value.Trim();[pscustomobject]@{ok=$true;ssid=$ssid;state=$state;signal=$signal}|ConvertTo-Json -Compress}}
"""
    elif action == "brightness_get":
        script = r"""
try {$b=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | Select-Object -First 1;if($null -eq $b){[pscustomobject]@{ok=$false;available=$false;error='Brightness control is unsupported by this display.'}|ConvertTo-Json -Compress}else{[pscustomobject]@{ok=$true;percent=$b.CurrentBrightness}|ConvertTo-Json -Compress}} catch {[pscustomobject]@{ok=$false;available=$false;error='Brightness control is unavailable on this display.'}|ConvertTo-Json -Compress}
"""
    elif action in ("brightness_set", "brightness_up", "brightness_down"):
        try:
            amount = int(value) if value else (10 if action == "brightness_up" else -10)
        except ValueError:
            return {"ok": False, "error": "Brightness must be a number from 0 to 100."}
        if action == "brightness_up": amount = max(0, min(100, amount))
        elif action == "brightness_down": amount = max(0, min(100, -amount))
        if action == "brightness_set" and not 0 <= amount <= 100:
            return {"ok": False, "error": "Brightness must be between 0 and 100."}
        script = f"""
try {{$b=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | Select-Object -First 1;if($null -eq $b){{throw 'unsupported'}};$target={amount};if('{action}' -eq 'brightness_up'){{$target=[math]::Min(100,$b.CurrentBrightness+{amount})}}elseif('{action}' -eq 'brightness_down'){{$target=[math]::Max(0,$b.CurrentBrightness-{amount})}};Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | ForEach-Object {{Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness=$target}}|Out-Null}};[pscustomobject]@{{ok=$true;percent=$target}}|ConvertTo-Json -Compress}} catch {{[pscustomobject]@{{ok=$false;available=$false;error='Brightness control is unavailable on this display.'}}|ConvertTo-Json -Compress}}
"""
    elif action == "lock_screen":
        script = r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class EVALock { [DllImport("user32.dll")] public static extern bool LockWorkStation(); }'
$r=[EVALock]::LockWorkStation();[pscustomobject]@{ok=[bool]$r;locked=[bool]$r}|ConvertTo-Json -Compress
"""
    elif action == "audio_devices":
        script = r"""$d=@(Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | Select-Object Name,Status,Manufacturer);[pscustomobject]@{ok=$true;devices=$d;note='Device enumeration only; microphone recording and speaker loopback tests are not performed.'}|ConvertTo-Json -Depth 4 -Compress"""
    elif action in ("mic_test", "speaker_test"):
        return {"ok": False, "available": False, "error": "Windows microphone/speaker loopback testing is not configured. Use audio devices to enumerate hardware; install a dedicated audio backend to perform capture tests."}
    elif action == "battery":
        script = r"""
$b=Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
if($null -eq $b){[pscustomobject]@{ok=$true;available=$false;message='No battery detected (desktop PC or unsupported firmware)'}|ConvertTo-Json -Compress}
else{[pscustomobject]@{ok=$true;available=$true;percent=$b.EstimatedChargeRemaining;status=$b.BatteryStatus}|ConvertTo-Json -Compress}
"""
    elif action in ("volume_get", "volume_set"):
        script = r"""
[pscustomobject]@{ok=$false;available=$false;error='Exact Windows volume level read/set needs an audio endpoint backend. Use volume up/down/mute, which work through system media keys.'}|ConvertTo-Json -Compress
"""
    elif action in ("volume_up", "volume_down", "mute", "media_play_pause", "media_next", "media_prev"):
        vk = {"volume_up":"0xAF","volume_down":"0xAE","mute":"0xAD",
              "media_play_pause":"0xB3","media_next":"0xB0","media_prev":"0xB1"}[action]
        script = f"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class EVAKey {{ [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo); }}'
[EVAKey]::keybd_event({vk},0,1,[UIntPtr]::Zero)
[EVAKey]::keybd_event({vk},0,3,[UIntPtr]::Zero)
[pscustomobject]@{{ok=$true;action='{action}'}}|ConvertTo-Json -Compress
"""
    elif action == "apps":
        script = r"""[pscustomobject]@{ok=$true;apps=@(Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object -ExpandProperty ProcessName -Unique | Sort-Object)}|ConvertTo-Json -Compress"""
    elif action == "processes":
        script = r"""
$ErrorActionPreference='Stop'
try {
  $items = @(Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 500 | ForEach-Object {
    [pscustomobject]@{name=$_.ProcessName;pid=$_.Id;cpu_seconds=if($null -ne $_.CPU){[math]::Round($_.CPU,2)}else{$null};working_set_bytes=$_.WorkingSet64;memory_mb=[math]::Round($_.WorkingSet64/1MB,1);handles=$_.Handles;responding=if($_.Responding -is [bool]){$_.Responding}else{$null}}
  })
  [pscustomobject]@{ok=$true;processes=$items;count=$items.Count;truncated=((Get-Process).Count -gt 500)} | ConvertTo-Json -Depth 4 -Compress
} catch { [pscustomobject]@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json -Compress }
"""
    elif action == "app_front":
        script = r"""Add-Type -AssemblyName Microsoft.VisualBasic; [pscustomobject]@{ok=$true;message='Windows does not reliably expose the foreground app name through the standard library.';available=$false}|ConvertTo-Json -Compress"""
    elif action == "app_open":
        folder_aliases = {
            "downloads folder": "Downloads", "downloads": "Downloads",
            "documents folder": "MyDocuments", "documents": "MyDocuments",
            "desktop folder": "Desktop", "desktop": "Desktop",
            "pictures folder": "MyPictures", "pictures": "MyPictures",
            "music folder": "MyMusic", "music folder": "MyMusic",
            "videos folder": "MyVideos", "videos": "MyVideos",
        }
        if value.strip().lower() in folder_aliases:
            known = folder_aliases[value.strip().lower()]
            qknown = "'" + known.replace("'", "''") + "'"
            script = f"""
$ErrorActionPreference = 'Stop'
try {{
  if ({qknown} -eq 'Downloads') {{
    $shell = New-Object -ComObject Shell.Application
    $downloads = $shell.Namespace('shell:Downloads')
    $dest = if ($downloads -and $downloads.Self) {{ $downloads.Self.Path }} else {{ Join-Path $env:USERPROFILE 'Downloads' }}
  }} else {{
    $dest = [Environment]::GetFolderPath({qknown})
    if (-not $dest) {{ $dest = Join-Path $env:USERPROFILE {qknown} }}
  }}
  if (-not (Test-Path -LiteralPath $dest)) {{ throw 'Requested folder was not found.' }}
  Start-Process -FilePath 'explorer.exe' -ArgumentList @($dest) -ErrorAction Stop
  [pscustomobject]@{{ok=$true;opened=$dest;launch_requested=$true}} | ConvertTo-Json -Compress
}} catch {{ [pscustomobject]@{{ok=$false;error=$_.Exception.Message}} | ConvertTo-Json -Compress }}
"""
        else:
            script = f"""
$ErrorActionPreference = 'Stop'
try {{
  Start-Process -FilePath {v} -ErrorAction Stop
  [pscustomobject]@{{ok=$true;opened={v};launch_requested=$true}} | ConvertTo-Json -Compress
}} catch {{ [pscustomobject]@{{ok=$false;error=$_.Exception.Message}} | ConvertTo-Json -Compress }}
"""
    elif action == "app_quit":
        # Only quit an exact process name; no wildcard or arbitrary script.
        safe = re.sub(r"[^A-Za-z0-9_. -]", "", value).strip()
        if not safe:
            return {"ok": False, "error": "Please provide an application/process name."}
        q = "'" + safe.replace("'", "''") + "'"
        script = f"""$p=Get-Process -Name {q} -ErrorAction SilentlyContinue; if($p){{$p | Stop-Process -ErrorAction Stop; [pscustomobject]@{{ok=$true;closed={q}}}|ConvertTo-Json -Compress}}else{{[pscustomobject]@{{ok=$true;running=$false;message='Process not found'}}|ConvertTo-Json -Compress}}"""
    elif action == "screenshot":
        path_obj = Path(value.strip()).expanduser().resolve(strict=False) if value and value.strip() else (Path.home() / "Pictures" / "EVA-screenshot.png").resolve(strict=False)
        try:
            from eva.automation.engine.policy import AutomationPolicy
            policy = AutomationPolicy.load_default()
            if not policy.is_path_allowed(path_obj):
                return {"ok": False, "error": f"Screenshot destination is outside allowed automation roots: {path_obj}"}
        except Exception:
            pass
        path = str(path_obj)
        pv = "'" + path.replace("'", "''") + "'"
        script = f"""
Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing
$bounds=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp=New-Object System.Drawing.Bitmap $bounds.Width,$bounds.Height
$g=[System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size)
$dest={pv}; $dir=[System.IO.Path]::GetDirectoryName($dest); if($dir){{[System.IO.Directory]::CreateDirectory($dir)|Out-Null}}
$bmp.Save($dest,[System.Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose()
[pscustomobject]@{{ok=$true;path=$dest}}|ConvertTo-Json -Compress
"""
    elif action == "clipboard_get":
        script = r"""Add-Type -AssemblyName System.Windows.Forms; [pscustomobject]@{ok=$true;text=[System.Windows.Forms.Clipboard]::GetText()}|ConvertTo-Json -Compress"""
    elif action == "clipboard_set":
        script = f"""Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::SetText({v}); $readback = [System.Windows.Forms.Clipboard]::GetText(); $ver = ($readback -eq {v}); [pscustomobject]@{{ok=$true;characters={len(value)};verified=$ver;value=$readback}}|ConvertTo-Json -Compress"""
    elif action == "type":
        # SendKeys text is fragile for Unicode and multi-line content. Paste the
        # exact Unicode string instead, then report dispatch honestly. The tool
        # contract declares clipboard.write because this briefly replaces it.
        # The prior clipboard contents are saved and restored after the paste so
        # that repeated calls or subsequent clipboard reads are not surprised by
        # EVA's transient payload. Restoration is best-effort: if SetText fails
        # (e.g. protected clipboard), the paste already completed successfully.
        encoded_text = base64.b64encode(value.encode("utf-8")).decode("ascii")
        target_proc = "notepad" if (target and "notepad" in target.lower()) else ""
        script = f"""
$ErrorActionPreference = 'Stop'
try {{
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    public class WinUtilFocus {{
      [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
      [DllImport("user32.dll", SetLastError = true)] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    }}
"@
  $fgHwnd = [WinUtilFocus]::GetForegroundWindow()
  $pidVal = [uint32]0
  [WinUtilFocus]::GetWindowThreadProcessId($fgHwnd, [ref]$pidVal) | Out-Null
  $fgProc = ''
  if ($pidVal -gt 0) {{
    $p = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
    if ($p) {{ $fgProc = $p.ProcessName.ToLower() }}
  }}
  $targetProc = '{target_proc}'.ToLower()
  if ($targetProc -ne '' -and $fgProc -ne '' -and $fgProc -ne $targetProc) {{
    [pscustomobject]@{{ok=$false;error="Focus was lost: target window was '$targetProc', but active window is '$fgProc'. Text paste aborted to prevent wrong-window entry.";error_code='FOCUS_LOST_BEFORE_TYPE';verified=$false}} | ConvertTo-Json -Compress
    exit
  }}
  $terminals = @('cmd','powershell','pwsh','windowsterminal','conhost','openconsole')
  if ($terminals -contains $fgProc) {{
    [pscustomobject]@{{ok=$false;error="Terminal window '$fgProc' is currently focused. Text paste aborted for safety.";error_code='TERMINAL_FOCUS_BLOCKED';verified=$false}} | ConvertTo-Json -Compress
    exit
  }}

  $text = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_text}'))
  $prior = ''
  try {{ $prior = [System.Windows.Forms.Clipboard]::GetText() }} catch {{}}
  [System.Windows.Forms.Clipboard]::SetText($text)
  Start-Sleep -Milliseconds 80
  [System.Windows.Forms.SendKeys]::SendWait('^v')
  Start-Sleep -Milliseconds 60
  $restored = $false
  try {{
    if ($prior -ne '') {{ [System.Windows.Forms.Clipboard]::SetText($prior) }} else {{ [System.Windows.Forms.Clipboard]::Clear() }}
    $restored = $true
  }} catch {{}}
  $msg = 'Paste shortcut dispatched; visible text was not independently read back.'
  if (-not $restored) {{
    $msg = 'Paste shortcut dispatched, but prior clipboard contents could not be restored.'
  }}
  [pscustomobject]@{{ok=$true;typed_characters={len(value)};method='unicode_clipboard_paste';clipboard_replaced=$true;clipboard_restored=$restored;verified=$false;message=$msg}} | ConvertTo-Json -Compress
}} catch {{
  [pscustomobject]@{{ok=$false;error=$_.Exception.Message;method='unicode_clipboard_paste';verified=$false}} | ConvertTo-Json -Compress
}}
"""
    elif action == "web":
        # Keep this branch safe even if called directly; execute() normally
        # intercepts Windows web actions and uses the cross-platform browser API.
        try:
            url = _normalise_web_target(value)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return _open_browser_url(url)
    elif action == "notify":
        script = f"""Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show({v},'EVA notification')|Out-Null; [pscustomobject]@{{ok=$true;shown=$true}}|ConvertTo-Json -Compress"""
    elif action == "say":
        script = f"""Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak({v}); [pscustomobject]@{{ok=$true;spoken=$true}}|ConvertTo-Json -Compress"""
    elif action == "keys":
        combo = value.lower().strip()
        mapkey = {"ctrl+l":"^l","ctrl+t":"^t","ctrl+w":"^w","ctrl+shift+t":"^+t",
                  "ctrl+tab":"^{TAB}","alt+tab":"%{TAB}","f11":"{F11}",
                  "space":" ","enter":"{ENTER}","escape":"{ESC}","ctrl+r":"^r",
                  "ctrl+shift+n":"^+n","ctrl+f":"^f","ctrl+c":"^c","ctrl+v":"^v",
                  "ctrl+x":"^x","ctrl+a":"^a","ctrl+z":"^z","ctrl+y":"^y",
                  "ctrl+shift+i":"^+i","ctrl+h":"^h","ctrl+d":"^d",
                  "ctrl+plus":"^+{=}","ctrl+-":"^-","alt+f":"%f","alt+left":"%{LEFT}","alt+right":"%{RIGHT}",
                  "up":"{UP}","down":"{DOWN}","left":"{LEFT}","right":"{RIGHT}",
                  "home":"{HOME}","end":"{END}","pageup":"{PGUP}","pagedown":"{PGDN}",
                  "l":"l","j":"j","shift+n":"+n","shift+p":"+p",
                  "shift+,":"+," ,"shift+.":"+.", "ctrl+left":"^{LEFT}","ctrl+right":"^{RIGHT}",
                  "ctrl+shift+tab":"^+{TAB}","ctrl+f":"^f"}
        if combo not in mapkey:
            return {"ok": False, "error": "Unsupported key combination. Supported examples: ctrl+l, ctrl+t, ctrl+w, ctrl+shift+t, alt+tab, f11, space, enter, ctrl+c/v/a/z/y."}
        ev = "'" + mapkey[combo].replace("'", "''") + "'"
        script = f"""Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait({ev}); [pscustomobject]@{{ok=$true;sent={v}}}|ConvertTo-Json -Compress"""
    else:
        return {"ok": False, "error": f"Unsupported Windows automation action: {action}"}
    return _ps(script, timeout=30, context=context)



def _create_file(request: str):
    """Create a new, empty file in a dedicated EVA folder without overwriting."""
    text = request.strip()
    types = {
        "python": ".py", "java": ".java", "text": ".txt", "html": ".html",
        "css": ".css", "javascript": ".js", "js": ".js", "json": ".json",
        "xml": ".xml", "csv": ".csv", "markdown": ".md", "yaml": ".yaml",
        "yml": ".yml", "pdf": ".pdf", "word": ".docx", "excel": ".xlsx",
        "powerpoint": ".pptx",
    }
    m = re.search(r"\b(?:create|make|new)\s+(?:a\s+)?(" + "|".join(types) + r")\s+file\b(.*)", text, re.I)
    if not m:
        return {"ok": False, "error": "Try 'create a Python file named hello' or 'create a text file named notes'."}
    kind, tail = m.group(1).lower(), m.group(2)
    name_match = re.search(r"\b(?:named|called|with name)\s+([A-Za-z0-9 _.-]+)", tail, re.I)
    name = name_match.group(1).strip(" .") if name_match else "untitled"
    # Never accept a path, traversal, or reserved filename from the prompt.
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9 _.-]", "", name).strip(" .")
    if not name or name in {".", ".."}:
        return {"ok": False, "error": "Please provide a valid filename."}
    extension = types[kind]
    if not name.lower().endswith(extension):
        name += extension
    folder = Path.home() / "Documents" / "EVA-created-files"
    try:
        from eva.automation.engine.policy import AutomationPolicy
        policy = AutomationPolicy.load_default()
        if policy.allowed_roots:
            for candidate in policy.allowed_roots:
                if candidate.exists() and candidate.is_dir():
                    folder = candidate / "EVA-created-files"
                    break
    except Exception:
        pass
    try:
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / name
        try:
            from eva.automation.engine.policy import AutomationPolicy
            policy = AutomationPolicy.load_default()
            if not policy.is_path_allowed(destination):
                return {"ok": False, "error": f"Path is outside allowed automation roots: {destination}"}
        except Exception:
            pass
        with destination.open("x", encoding="utf-8") as handle:
            handle.write("")
        return {"ok": True, "path": str(destination), "created": True}
    except FileExistsError:
        return {"ok": False, "error": f"{name} already exists; no file was overwritten.", "path": str(folder / name)}
    except OSError as exc:
        return {"ok": False, "error": f"Could not create file: {exc}"}


def _foreground_window_details() -> dict[str, str]:
    """Return the foreground window title and owning executable name."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"title": "", "process": ""}
    title_buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process_handle = kernel32.OpenProcess(0x1000, False, process_id.value)
    process_name = ""
    if process_handle:
        try:
            image_buffer = ctypes.create_unicode_buffer(32768)
            image_size = ctypes.c_ulong(len(image_buffer))
            if kernel32.QueryFullProcessImageNameW(
                process_handle, 0, image_buffer, ctypes.byref(image_size)
            ):
                process_name = Path(image_buffer.value).name.casefold()
        finally:
            kernel32.CloseHandle(process_handle)
    return {"title": title_buffer.value, "process": process_name}


def _focus_notepad_window(timeout: float = 8.0) -> dict[str, object]:
    """Focus the Notepad window and verify that Notepad owns the foreground."""
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        return {"ok": False, "error_code": "DEPENDENCY_MISSING",
                "error": f"Notepad focus needs pywinauto: {exc}"}

    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            windows = [
                window for window in Desktop(backend="win32").windows(visible_only=True)
                if ("notepad" in (window.window_text() or "").casefold()
                    and "hook" not in (window.class_name() or "").casefold())
            ]
            for window in windows:
                try:
                    window.restore()
                except Exception:
                    pass
                try:
                    window.set_keyboard_focus()
                except Exception:
                    pass
                try:
                    hwnd = getattr(window, "handle", None)
                    if hwnd:
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                try:
                    window.set_focus()
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(0.12)
                try:
                    foreground = _foreground_window_details()
                    if "notepad" in foreground.get("title", "").casefold() or foreground.get("process") == "notepad.exe":
                        return {"ok": True, "verified": True,
                                "window_title": window.window_text(),
                                "method": "pywinauto_window_focus"}
                except Exception as exc:
                    last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.15)
    window_found = bool(windows) if 'windows' in locals() and windows else False
    return {"ok": False, "verified": False, "error_code": "FOCUS_FAILED",
            "window_found": window_found,
            "error": "EVA could not verify that Notepad is the foreground window; no text was pasted."
                    + (f" ({last_error})" if last_error else "")}


def _new_notepad_document() -> dict[str, object]:
    """Create a new blank Notepad tab without altering the existing document.

    The key sequence is sent through pywinauto to the verified Notepad window,
    rather than starting a separate PowerShell process which might steal focus.
    If a Save/Save As dialog appears, fail closed so later text is never pasted
    into a modal dialog.
    """
    if platform.system().lower() != "windows":
        return {"ok": False, "verified": False, "error_code": "UNSUPPORTED_PLATFORM",
                "error": "Creating a fresh Notepad document is supported only on Windows."}
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        return {"ok": False, "verified": False, "error_code": "DEPENDENCY_MISSING",
                "error": f"A fresh Notepad document needs pywinauto: {exc}"}

    focus = _focus_notepad_window()
    if not focus.get("ok"):
        return focus
    try:
        windows = [window for window in Desktop(backend="win32").windows(visible_only=True)
                   if "notepad" in (window.window_text() or "").casefold()]
        preferred_title = str(focus.get("window_title", "")).casefold()
        windows.sort(key=lambda window: 0 if (window.window_text() or "").casefold() == preferred_title else 1)
        if not windows:
            return {"ok": False, "verified": False, "error_code": "NOTEPAD_WINDOW_MISSING",
                    "error": "Notepad's window disappeared before a new document could be opened."}
        target = windows[0]
        try:
            target.restore()
        except Exception:
            pass
        try:
            target.set_keyboard_focus()
        except Exception:
            pass
        try:
            hwnd = getattr(target, "handle", None)
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        try:
            target.set_focus()
        except Exception:
            pass
        time.sleep(0.1)
        foreground = _foreground_window_details()
        if "notepad" not in foreground.get("title", "").casefold() and foreground.get("process", "") != "notepad.exe":
            if foreground.get("title") or foreground.get("process"):
                return {"ok": False, "verified": False, "error_code": "FOCUS_FAILED",
                        "error": "Notepad is not foreground; no new document was requested."}
        target.type_keys("^n", set_foreground=True)
        time.sleep(0.25)
        current = _foreground_window_details()
        current_title = current.get("title", "")
        if re.search(r"\b(?:save|save as|save changes)\b", current_title, re.I):
            return {"ok": False, "verified": False, "error_code": "NOTEPAD_SAVE_DIALOG",
                    "error": "Notepad displayed a save dialog. EVA did not paste text into it; resolve the dialog and retry."}
        if "notepad" not in current_title.casefold() and current.get("process", "") != "notepad.exe":
            return {"ok": False, "verified": False, "error_code": "NOTEPAD_FOCUS_LOST",
                    "error": "The new-document shortcut was sent, but Notepad is no longer foreground; text entry was stopped."}
        blank_check = _verify_notepad_text("", timeout=1.0)
        if not blank_check.get("verified"):
            return {"ok": False, "verified": False, "error_code": "NOTEPAD_NEW_DOCUMENT_UNVERIFIED",
                    "method": "notepad_focused_ctrl_n",
                    "error": "Ctrl+N was dispatched to Notepad, but EVA could not independently verify a blank document. Text entry was stopped to avoid appending into an existing note."}
        return {"ok": True, "verified": True, "method": "notepad_focused_ctrl_n",
                "message": "A blank Notepad document was read back before any new content was entered."}
    except Exception as exc:
        return {"ok": False, "verified": False, "error_code": "NOTEPAD_NEW_DOCUMENT_FAILED",
                "error": f"Could not create a new Notepad document ({type(exc).__name__}). No text was pasted."}


def _verify_notepad_text(expected: str, timeout: float = 1.8) -> dict[str, object]:
    """Best-effort UI Automation read-back of the Notepad document text.

    A successful paste dispatch is not proof of document contents. This helper
    reads the accessible Document/Edit value when Windows exposes it; callers
    must preserve an explicit unverified result when the control is unavailable.
    """
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        return {"readable": False, "verified": False,
                "message": f"Notepad text read-back requires pywinauto: {exc}"}

    def normalize(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    wanted = normalize(expected)
    deadline = time.monotonic() + max(0.1, timeout)
    saw_readable_control = False
    last_value = ""
    last_error = None
    while time.monotonic() < deadline:
        try:
            windows = [window for window in Desktop(backend="uia").windows(visible_only=True)
                       if "notepad" in (window.window_text() or "").casefold()]
            for window in windows:
                try:
                    controls = window.descendants()
                except Exception as exc:
                    last_error = str(exc)
                    continue
                for control in controls:
                    try:
                        control_type = str(getattr(control.element_info, "control_type", ""))
                    except Exception:
                        control_type = ""
                    if control_type not in {"Document", "Edit"}:
                        continue
                    candidates: list[str] = []
                    try:
                        getter = getattr(control, "get_value", None)
                        if callable(getter):
                            candidates.append(str(getter() or ""))
                    except Exception as exc:
                        last_error = str(exc)
                    try:
                        iface_value = getattr(control, "iface_value", None)
                        if iface_value is not None:
                            candidates.append(str(iface_value.CurrentValue or ""))
                    except Exception as exc:
                        last_error = str(exc)
                    try:
                        iface_text = getattr(control, "iface_text", None)
                        if iface_text is not None:
                            text_pattern = getattr(iface_text, "DocumentRange", None)
                            if text_pattern is not None:
                                candidates.append(str(text_pattern.GetText(-1) or ""))
                    except Exception as exc:
                        last_error = str(exc)
                    try:
                        candidates.append(str(control.window_text() or ""))
                    except Exception as exc:
                        last_error = str(exc)
                    # Prefer the fullest candidate to avoid accepting a title or
                    # partial accessible fragment when the control exposes both.
                    candidates = sorted(set(candidates), key=len, reverse=True)
                    for candidate in candidates:
                        if not candidate and not wanted:
                            saw_readable_control = True
                            return {"readable": True, "verified": True, "characters": 0,
                                    "message": "Notepad's accessible document is empty as expected."}
                        if candidate:
                            saw_readable_control = True
                            last_value = candidate
                            if normalize(candidate) == wanted:
                                return {"readable": True, "verified": True, "characters": len(candidate),
                                        "message": "Notepad document text was read back and exactly matched the requested text."}
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.12)

    if saw_readable_control:
        return {"readable": True, "verified": False, "characters": len(last_value),
                "message": "Notepad's accessible document text did not exactly match the requested text; the paste result is not accepted as verified."}
    return {"readable": False, "verified": False,
            "message": "Paste was dispatched, but Windows did not expose readable Notepad document text for independent verification."
                    + (f" ({last_error})" if last_error else "")}


def calculate_in_windows_calculator(expression: str) -> dict[str, object]:
    """Enter arithmetic into Windows Calculator and verify its accessible display.

    Prefer Calculator UIA buttons; if an OS build exposes different automation
    IDs, fall back to keystrokes only after the Calculator window is confirmed as
    the foreground window. The function never treats EVA's independent math
    result alone as proof that the UI calculation succeeded.
    """
    normalized = expression.strip().rstrip(".!? ")
    normalized = re.sub(r"\b(?:calculate|calulate|what is|compute|evaluate)\b", "", normalized, flags=re.I)
    word_ops = ((r"\bmultiplied\s+by\b|\btimes\b|\bmultiplied\b", "*"),
                (r"\bdivided\s+by\b|\bdivide(?:d)?\s+by\b", "/"),
                (r"\bplus\b|\badd(?:ed)?\b", "+"),
                (r"\bminus\b|\bsubtract(?:ed)?\b", "-"))
    for pattern, symbol in word_ops:
        normalized = re.sub(pattern, symbol, normalized, flags=re.I)
    normalized = re.sub(r"\b(?:and|then)\b", " ", normalized, flags=re.I)
    normalized = re.sub(r"(?i)(?<=\d)\s*[x×]\s*(?=\d)", "*", normalized)
    normalized = normalized.replace("×", "*").replace("÷", "/")
    normalized = re.sub(r"\s+", "", normalized)
    try:
        tree = ast.parse(normalized, mode="eval").body
        binary_ops = {ast.Add: operator.add, ast.Sub: operator.sub,
                      ast.Mult: operator.mul, ast.Div: operator.truediv}

        def evaluate(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
                return binary_ops[type(node.op)](evaluate(node.left), evaluate(node.right))
            raise ValueError("Only numeric addition, subtraction, multiplication, and division are supported.")

        expected_value = evaluate(tree)
        if not math.isfinite(expected_value):
            raise ValueError("The result is not finite.")
        tokens = re.findall(r"\d+(?:\.\d+)?|[+*/-]", normalized)
        if not tokens or "".join(tokens) != normalized or len(tokens) % 2 != 1:
            raise ValueError("Use a simple sequence of numbers and +, -, *, or / operators.")
        if any(tokens[index] not in {"+", "-", "*", "/"} for index in range(1, len(tokens), 2)):
            raise ValueError("The expression contains an unsupported operator sequence.")
        if any(tokens[index] == "/" and float(tokens[index + 1]) == 0 for index in range(1, len(tokens), 2)):
            raise ValueError("Division by zero is not supported.")
        if any(tokens[index] in {"*", "/"} for index in range(1, len(tokens), 2)) and any(tokens[index] in {"+", "-"} for index in range(1, len(tokens), 2)):
            raise ValueError("Windows Calculator Standard mode calculates left-to-right without operator precedence (e.g. 2+3*4 evaluates as 20 instead of 14). Combine operations of one precedence at a time in Calculator, or use EVA's math engine for full PEMDAS evaluation.")
        if len(tokens) > 11:
            raise ValueError("The Calculator entry is limited to six numbers per expression.")
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return {"ok": False, "verified": False, "error_code": "UNSUPPORTED_EXPRESSION",
                "error": str(exc) or "The expression is not supported."}

    expected = str(int(expected_value)) if expected_value.is_integer() else f"{expected_value:.12g}"

    def display_text(calculator_window) -> str:
        pieces: list[str] = []
        try:
            result_control = calculator_window.child_window(auto_id="CalculatorResults", control_type="Text")
            for getter in (lambda: result_control.window_text(),
                           lambda: result_control.element_info.name,
                           lambda: result_control.iface_text.CurrentName):
                try:
                    value = str(getter() or "").strip()
                    if value and value not in pieces:
                        pieces.append(value)
                except Exception:
                    pass
        except Exception:
            pass
        # Windows Calculator's accessible result AutomationId is stable on most
        # builds, but its text control type/name varies on some Windows versions.
        if not pieces:
            try:
                for control in calculator_window.descendants(control_type="Text"):
                    for getter in (lambda c=control: c.window_text(),
                                   lambda c=control: c.element_info.name):
                        try:
                            value = str(getter() or "").strip()
                            if value and value not in pieces:
                                pieces.append(value)
                        except Exception:
                            pass
            except Exception:
                pass
        return " | ".join(pieces)

    def display_matches(label: str) -> bool:
        if not label:
            return False
        # Prefer the last numeric value in accessible display strings such as
        # "Display is 105", "105", or grouped values like "1,024".
        matches = re.findall(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?", label)
        if not matches:
            return False
        try:
            displayed = float(matches[-1].replace(",", ""))
            return math.isclose(displayed, expected_value, rel_tol=1e-10, abs_tol=1e-10)
        except (ValueError, OverflowError):
            return False

    try:
        from pywinauto import Desktop, keyboard
        calculator = Desktop(backend="uia").window(title_re=r"(?i).*calculator.*")
        calculator.wait("exists visible enabled", timeout=6)
        calculator.set_focus()
        time.sleep(0.18)
        foreground = _foreground_window_details()
        if not ("calculator" in foreground.get("title", "").casefold()
                or "calculator" in foreground.get("process", "").casefold()):
            return {"ok": False, "verified": False, "error_code": "CALCULATOR_FOCUS_FAILED",
                    "expected": expected,
                    "foreground_title": foreground.get("title", ""),
                    "error": "EVA could not confirm Calculator was the foreground window; no calculation was entered."}

        button_error = ""
        method = "calculator_uia_buttons"
        try:
            def press(auto_id: str) -> None:
                calculator.child_window(auto_id=auto_id, control_type="Button").invoke()

            digits = {str(i): f"num{i}Button" for i in range(10)}
            op_buttons = {"+": "plusButton", "-": "minusButton",
                          "*": "multiplyButton", "/": "divideButton"}
            press("clearButton")
            for index, token in enumerate(tokens):
                if index % 2:
                    press(op_buttons[token])
                else:
                    for character in token:
                        press("decimalSeparatorButton" if character == "." else digits[character])
            press("equalButton")
        except Exception as exc:
            button_error = f"{type(exc).__name__}: {exc}"[:240]
            method = "calculator_keyboard_fallback"
            try:
                # The foreground check above is mandatory before any keystroke.
                # Escape clears a partially entered expression if the button path
                # failed midway. Escape each arithmetic character as a named key
                # where pywinauto uses modifier syntax for punctuation.
                keyboard.send_keys("{ESC}", pause=0.02)
                key_ops = {"+": "{ADD}", "-": "{SUBTRACT}", "*": "{MULTIPLY}", "/": "{DIVIDE}"}
                for index, token in enumerate(tokens):
                    if index % 2:
                        keyboard.send_keys(key_ops[token], pause=0.025)
                    else:
                        for character in token:
                            keyboard.send_keys(character, pause=0.015)
                keyboard.send_keys("{ENTER}", pause=0.02)
            except Exception as fallback_exc:
                return {"ok": False, "verified": False, "error_code": "CALCULATOR_UI_UNAVAILABLE",
                        "expected": expected, "method": method,
                        "button_path_error": button_error,
                        "error": f"Calculator UI buttons and keyboard fallback both failed: {type(fallback_exc).__name__}: {fallback_exc}"[:500]}

        deadline = time.monotonic() + 4.0
        label = ""
        while time.monotonic() < deadline:
            label = display_text(calculator)
            if display_matches(label):
                return {"ok": True, "verified": True, "expression": normalized,
                        "display": label, "result": expected, "method": method,
                        **({"button_path_error": button_error} if button_error else {})}
            time.sleep(0.12)
        return {"ok": False, "verified": False, "error_code": "CALCULATOR_RESULT_MISMATCH",
                "expression": normalized, "display": label, "expected": expected, "method": method,
                **({"button_path_error": button_error} if button_error else {}),
                "error": "Calculator's visible result could not be matched to the independently computed result."}
    except Exception as exc:
        return {"ok": False, "verified": False, "error_code": "CALCULATOR_UI_UNAVAILABLE",
                "expected": expected,
                "error": f"Calculator could not be operated through its accessible controls: {type(exc).__name__}: {exc}"}


def _try_open_native_app(target: str):
    """Open a known installed app, returning None when browser fallback is needed.

    On Windows, Start menu apps must be launched through Explorer's AppsFolder
    namespace. Starting an AppsFolder URI as if it were an executable can
    fail, and older code accidentally reported success because PowerShell kept
    running after a non-terminating Start-Process error.
    """
    key = target.strip().lower()
    key = {"vscode": "vs code", "visual studio code": "vs code"}.get(key, key)
    apps = {
        "spotify": {"display": ["Spotify", "Spotify Music"], "executables": ["Spotify.exe"], "protocol": "spotify"},
        "whatsapp": {"display": ["WhatsApp", "WhatsApp Beta"], "executables": ["WhatsApp.exe"], "protocol": "whatsapp"},
        "instagram": {"display": ["Instagram"], "executables": ["Instagram.exe"], "protocol": ""},
        "facebook": {"display": ["Facebook"], "executables": ["Facebook.exe"], "protocol": ""},
        "youtube": {"display": ["YouTube"], "executables": ["YouTube.exe"], "protocol": ""},
        "notepad": {"display": ["Notepad"], "executables": ["notepad.exe"], "protocol": ""},
        "camera": {"display": ["Camera", "Windows Camera"], "executables": ["WindowsCamera.exe"], "protocol": "microsoft.windows.camera"},
        "calculator": {"display": ["Calculator", "Windows Calculator"], "executables": ["calc.exe"], "protocol": "calculator"},
        "paint": {"display": ["Paint", "Microsoft Paint"], "executables": ["mspaint.exe"], "protocol": ""},
        "chrome": {"display": ["Google Chrome", "Chrome"], "executables": ["chrome.exe"], "protocol": ""},
        "google chrome": {"display": ["Google Chrome", "Chrome"], "executables": ["chrome.exe"], "protocol": ""},
        "edge": {"display": ["Microsoft Edge", "Edge"], "executables": ["msedge.exe"], "protocol": ""},
        "microsoft edge": {"display": ["Microsoft Edge", "Edge"], "executables": ["msedge.exe"], "protocol": ""},
        "firefox": {"display": ["Firefox", "Mozilla Firefox"], "executables": ["firefox.exe"], "protocol": ""},
        "vlc": {"display": ["VLC media player", "VLC"], "executables": ["vlc.exe"], "protocol": ""},
        "discord": {"display": ["Discord"], "executables": ["Discord.exe"], "protocol": "discord"},
        "telegram": {"display": ["Telegram Desktop", "Telegram"], "executables": ["Telegram.exe"], "protocol": "tg"},
        "zoom": {"display": ["Zoom Workplace", "Zoom"], "executables": ["Zoom.exe"], "protocol": "zoommtg"},
        "slack": {"display": ["Slack"], "executables": ["slack.exe"], "protocol": "slack"},
        "steam": {"display": ["Steam"], "executables": ["steam.exe"], "protocol": "steam"},
        "vs code": {"display": ["Visual Studio Code", "Visual Studio Code Insiders", "VS Code"], "executables": ["Code.exe"], "protocol": "vscode"},
        "microsoft teams": {"display": ["Microsoft Teams", "Teams"], "executables": ["ms-teams.exe", "Teams.exe"], "protocol": "msteams"},
        "teams": {"display": ["Microsoft Teams", "Teams"], "executables": ["ms-teams.exe", "Teams.exe"], "protocol": "msteams"},
    }
    spec = apps.get(key)
    if not spec:
        return None

    system = platform.system().lower()
    if system == "windows":
        # Notepad, Calculator, and Paint can be launched through their native
        # executable paths instead of PowerShell/Start-Process. This avoids
        # provider-drive failures in inherited shell contexts for core Windows apps.
        # Notepad's WindowsApps execution alias is intentionally bypassed.
        if key in {"notepad", "calculator", "paint"}:
            windir = os.environ.get("WINDIR", r"C:\Windows")
            system32 = os.path.join(windir, "System32")
            executable = {"notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe"}[key]
            candidates = [os.path.join(system32, executable)]
            located = shutil.which(executable)
            if located:
                candidates.append(located)
            tried = []
            for candidate in dict.fromkeys(candidates):
                if not os.path.isfile(candidate):
                    continue
                try:
                    subprocess.Popen([candidate], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     close_fds=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    display_name = {"notepad": "Notepad", "calculator": "Calculator", "paint": "Paint"}[key]
                    result = {"ok": True, "opened": display_name,
                              "method": "native_process_launch", "launch_requested": True,
                              "verified": False, "path": candidate,
                              "message": "Windows accepted the application launch request; visible-window state was not independently verified."}
                    if key == "notepad":
                        focus = _focus_notepad_window()
                        result.update({"focus_verified": bool(focus.get("verified")),
                                       "verified": bool(focus.get("verified")),
                                       "window_title": focus.get("window_title"),
                                       "focus_method": focus.get("method")})
                        if not focus.get("ok"):
                            if focus.get("window_found"):
                                result.update({"ok": True, "verified": False})
                            else:
                                result.update({"ok": False,
                                               "error_code": focus.get("error_code", "FOCUS_FAILED"),
                                               "error": focus.get("error", "Notepad launch was requested but its foreground window could not be verified.")})
                    return result
                except (OSError, subprocess.SubprocessError) as exc:
                    tried.append(f"{candidate}: {type(exc).__name__}: {exc}")
            return {"ok": False, "not_found": not tried,
                    "error_code": "APP_LAUNCH_FAILED" if tried else "APP_NOT_INSTALLED",
                    "error": "Could not launch the Windows application directly." + (" " + "; ".join(tried) if tried else " No executable was found in System32 or PATH."),
                    "launch_requested": False}
        display_values = ",".join("'" + name.replace("'", "''") + "'" for name in spec["display"])
        executable_values = ",".join("'" + name.replace("'", "''") + "'" for name in spec["executables"])
        protocol = spec.get("protocol", "")
        quoted_protocol = "'" + protocol.replace("'", "''") + "'"
        script = rf"""
$ErrorActionPreference = 'Stop'
$displayNames = @({display_values})
$exeNames = @({executable_values})
$lastError = $null
try {{ $startApps = @(Get-StartApps -ErrorAction SilentlyContinue) }} catch {{ $startApps = @() }}
foreach ($name in $displayNames) {{
    $app = $startApps | Where-Object {{ $_.Name -eq $name -or $_.Name -like ($name + '*') }} | Select-Object -First 1
    if ($app -and $app.AppID) {{
        try {{
            $explorer = Join-Path $env:WINDIR 'explorer.exe'
            $appFolderTarget = 'shell:AppsFolder' + [char]92 + $app.AppID
            Start-Process -FilePath $explorer -ArgumentList @($appFolderTarget) -ErrorAction Stop
            [pscustomobject]@{{ok=$true;opened=$app.Name;method='installed_app';launch_requested=$true;app_id=$app.AppID}} | ConvertTo-Json -Compress
            exit 0
        }} catch {{ $lastError = $_.Exception.Message }}
    }}
}}
foreach ($exeName in $exeNames) {{
    $commands = @(Get-Command -Name $exeName -CommandType Application -ErrorAction SilentlyContinue)
    foreach ($cmd in $commands) {{
        if ($cmd -and $cmd.Source) {{
        # The Notepad execution alias under WindowsApps can open Microsoft Store
        # when the packaged app is missing. Never follow that alias as a fallback.
        if ({'$true' if key == 'notepad' else '$false'} -and $cmd.Source -match '(?i)\\WindowsApps\\') {{ continue }}
        try {{
            Start-Process -FilePath $cmd.Source -ErrorAction Stop
            [pscustomobject]@{{ok=$true;opened=$exeName;method='installed_app';launch_requested=$true;path=$cmd.Source}} | ConvertTo-Json -Compress
            exit 0
        }} catch {{ $lastError = $_.Exception.Message }}
        }}
    }}
}}
if ({'$true' if key == 'vs code' else '$false'}) {{
    $candidatePaths = @()
    if ($env:LOCALAPPDATA) {{ $candidatePaths += (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe') }}
    if ($env:ProgramFiles) {{ $candidatePaths += (Join-Path $env:ProgramFiles 'Microsoft VS Code\Code.exe') }}
    $pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    if ($pf86) {{ $candidatePaths += (Join-Path $pf86 'Microsoft VS Code\Code.exe') }}
    foreach ($candidate in $candidatePaths) {{
        if (Test-Path -LiteralPath $candidate) {{
            try {{
                Start-Process -FilePath $candidate -ErrorAction Stop
                [pscustomobject]@{{ok=$true;opened='Visual Studio Code';method='installed_app';launch_requested=$true;path=$candidate}} | ConvertTo-Json -Compress
                exit 0
            }} catch {{ $lastError = $_.Exception.Message }}
        }}
    }}
}}
$protocol = {quoted_protocol}
if ($protocol) {{
    $schemeKey = 'Registry::HKEY_CLASSES_ROOT' + [char]92 + $protocol
    if (Test-Path -LiteralPath $schemeKey) {{
        try {{
            Start-Process -FilePath ($protocol + ':') -ErrorAction Stop
            [pscustomobject]@{{ok=$true;opened=$protocol;method='installed_app_protocol';launch_requested=$true}} | ConvertTo-Json -Compress
            exit 0
        }} catch {{ $lastError = $_.Exception.Message }}
    }}
}}
[pscustomobject]@{{ok=$false;not_found=($null -eq $lastError);error=$lastError;message='No launchable installed app was found.'}} | ConvertTo-Json -Compress
"""
        result = _ps(script, timeout=12)
        if result.get("ok"):
            if key == "notepad":
                focus = _focus_notepad_window()
                result.update({"focus_verified": bool(focus.get("verified")),
                               "verified": bool(focus.get("verified")),
                               "window_title": focus.get("window_title"),
                               "focus_method": focus.get("method")})
                if not focus.get("ok"):
                    result.update({"ok": False,
                                   "error_code": focus.get("error_code", "FOCUS_FAILED"),
                                   "error": focus.get("error")})
            return result
        # Let execute() try the browser fallback. Preserve a hard launch error
        # for the caller so it is not mistaken for a successful native launch.
        if not result.get("not_found"):
            return {"ok": False, "method": "installed_app", "error": result.get("error") or result.get("message") or "Native app launch failed."}
        return None

    if system == "darwin":
        for app_name in spec["display"]:
            try:
                p = subprocess.run(["open", "-a", app_name], capture_output=True, text=True, timeout=8)
                if p.returncode == 0:
                    return {"ok": True, "opened": app_name, "method": "installed_app", "launch_requested": True}
            except (OSError, subprocess.TimeoutExpired):
                continue
        return None
    return None


def _normalise_web_target(value: str) -> str:
    """Turn an alias, URL, or search phrase into a safe browser URL."""
    value = value.strip()
    if not value:
        raise ValueError("No website or search phrase was provided.")
    aliases = _website_aliases()
    alias_url = aliases.get(value.lower())
    if alias_url:
        return alias_url
    if re.match(r"^https?://", value, re.I):
        # Never pass malformed URLs, embedded credentials, or control characters
        # to the OS browser handler. Local HTTP URLs remain available when the
        # user explicitly requests them (e.g. a local router/admin page).
        from urllib.parse import urlsplit
        if any(ord(char) < 32 for char in value):
            raise ValueError("URLs cannot contain control characters.")
        parsed = urlsplit(value)
        if not parsed.hostname:
            raise ValueError("The URL must include a hostname.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URLs containing embedded credentials are not supported.")
        return value
    if re.match(r"^(?:www\.)?[\w.-]+\.[a-z]{2,}(?:/.*)?$", value, re.I):
        return "https://" + value if not value.lower().startswith("www.") else "https://" + value
    return "https://www.google.com/search?q=" + quote_plus(value)


def _open_browser_url(url: str) -> dict:
    """Open a URL as a tab, reusing an installed/running Chromium browser on Windows."""
    errors = []
    if platform.system().lower() == "windows":
        # webbrowser.open(new=2) is advisory and some Windows registrations
        # interpret it as a separate window. Explicit --new-tab makes compound
        # commands (Wikipedia + Drive + other sites) share the same browser.
        candidates = []
        for name in ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"):
            found = shutil.which(name)
            if found:
                candidates.append((name, found))
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                         os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")]
        known_paths = [
            ("chrome.exe", os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")),
            ("msedge.exe", os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe")),
            ("brave.exe", os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
        ]
        for root in program_files:
            known_paths.extend([
                ("chrome.exe", os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")),
                ("msedge.exe", os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")),
                ("brave.exe", os.path.join(root, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
            ])
        for name, path in known_paths:
            if path and os.path.isfile(path) and not any(n == name for n, _ in candidates):
                candidates.append((name, path))
        # Prefer the browser already running so every URL joins its existing window.
        running = set()
        try:
            tasklist = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                      text=True, timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if tasklist.returncode == 0:
                running = {name.lower() for name in re.findall(r'"([^"\r\n]+\.exe)"', tasklist.stdout, re.I)}
        except (OSError, subprocess.TimeoutExpired):
            pass
        candidates.sort(key=lambda pair: (pair[0].lower() not in running, ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe").index(pair[0])))
        for name, executable in candidates:
            try:
                subprocess.Popen([executable, "--new-tab", url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return {"ok": True, "opened": url, "url": url, "method": "browser_new_tab",
                        "browser": name.removesuffix(".exe"), "launch_requested": True}
            except OSError as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        if webbrowser.open(url, new=2, autoraise=True):
            return {"ok": True, "opened": url, "url": url, "method": "browser", "launch_requested": True}
        errors.append("webbrowser.open returned False")
    except Exception as exc:
        errors.append(f"webbrowser.open: {type(exc).__name__}: {exc}")

    if platform.system().lower() == "windows":
        startfile = getattr(os, "startfile", None)
        if callable(startfile):
            try:
                startfile(url)
                return {"ok": True, "opened": url, "url": url, "method": "windows_shell_fallback", "launch_requested": True}
            except OSError as exc:
                errors.append(f"os.startfile: {type(exc).__name__}: {exc}")
    return {"ok": False, "url": url, "method": "browser", "error": "Could not request the browser to open this URL. " + "; ".join(errors)}


def _play_youtube(song_name: str):
    """Use pywhatkit to launch the requested YouTube song, with a safe web fallback."""
    song = song_name.strip().strip("\"'")
    if not song:
        return {"ok": False, "error": "Please include a song or video name."}
    try:
        import pywhatkit as pw
        pw.playonyt(song)
        return {"ok": True, "action": "play_youtube", "query": song,
                "method": "pywhatkit", "playback_requested": True,
                "note": "pywhatkit was asked to open and play the matching YouTube video; playback can still depend on browser/device state."}
    except ImportError:
        # Keep EVA useful before optional requirements have been installed.
        url = "https://www.youtube.com/results?search_query=" + quote_plus(song)
        try:
            opened = webbrowser.open(url)
            return {"ok": bool(opened), "action": "play_youtube", "query": song,
                    "method": "browser_search_fallback", "playback_requested": False,
                    "url": url, "note": "pywhatkit is not installed; opened YouTube search results instead."}
        except Exception as exc:
            return {"ok": False, "error": f"pywhatkit is not installed and browser fallback failed: {exc}"}
    except Exception as exc:
        # Do not claim playback succeeded if pywhatkit itself failed.
        return {"ok": False, "action": "play_youtube", "query": song,
                "error": f"pywhatkit could not start YouTube playback: {type(exc).__name__}: {exc}"}


def _launch_spotify_app_search(song: str):
    """Open a song search in the native Spotify app, or report app absence.

    This only requests the search page. The no-API integration does not select a
    track automatically; the user may choose the desired result in Spotify.
    """
    encoded_query = quote(song, safe="")
    app_uri = f"spotify:search:{encoded_query}"
    system = platform.system().lower()

    if system == "windows":
        uri_literal = "'" + app_uri.replace("'", "''") + "'"
        script = f"""
$ErrorActionPreference = 'Stop'
$uri = {uri_literal}
$schemeKey = 'Registry::HKEY_CLASSES_ROOT' + [char]92 + 'spotify'
$schemeRegistered = Test-Path -LiteralPath $schemeKey
$startApps = @()
try {{ $startApps = @(Get-StartApps -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -match '^Spotify(\\b| Music|$)' }}) }} catch {{}}
$exeCandidates = @()
if ($env:APPDATA) {{ $exeCandidates += (Join-Path $env:APPDATA 'Spotify\\Spotify.exe') }}
if ($env:LOCALAPPDATA) {{ $exeCandidates += (Join-Path $env:LOCALAPPDATA 'Spotify\\Spotify.exe') }}
if ($env:LOCALAPPDATA) {{ $exeCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\\Spotify\\Spotify.exe') }}
$exe = Get-Command -Name 'Spotify.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($exe -and $exe.Source) {{ $exeCandidates += $exe.Source }}
$exePath = $exeCandidates | Where-Object {{ $_ -and (Test-Path -LiteralPath $_) }} | Select-Object -First 1
if (-not $schemeRegistered -and -not $exePath -and $startApps.Count -eq 0) {{
  [pscustomobject]@{{ok=$false;not_found=$true;method='spotify_app_search';error='Spotify desktop app was not found.'}} | ConvertTo-Json -Compress
  exit 0
}}
try {{
  if ($schemeRegistered) {{
    Start-Process -FilePath $uri -ErrorAction Stop
    [pscustomobject]@{{ok=$true;method='spotify_app_search';opened=$uri;app_detected=$true;launch_requested=$true;playback_requested=$false;note='Opened Spotify desktop search. Select the desired track in Spotify; automated account playback is not integrated.'}} | ConvertTo-Json -Compress
    exit 0
  }}
  if ($exePath) {{
    Start-Process -FilePath $exePath -ArgumentList @($uri) -ErrorAction Stop
    [pscustomobject]@{{ok=$true;method='spotify_app_search';opened=$uri;app_detected=$true;launch_requested=$true;playback_requested=$false;note='Opened Spotify desktop search.'}} | ConvertTo-Json -Compress
    exit 0
  }}
  $app = $startApps | Select-Object -First 1
  if ($app -and $app.AppID) {{
    $explorer = Join-Path $env:WINDIR 'explorer.exe'
    Start-Process -FilePath $explorer -ArgumentList @('shell:AppsFolder' + [char]92 + $app.AppID) -ErrorAction Stop
    [pscustomobject]@{{ok=$false;not_found=$false;method='spotify_app_open';error='Spotify app opened, but its search URI handler is not registered.'}} | ConvertTo-Json -Compress
    exit 0
  }}
  [pscustomobject]@{{ok=$false;not_found=$false;method='spotify_app_search';error='Spotify is installed but could not open the song search URI.'}} | ConvertTo-Json -Compress
}} catch {{ [pscustomobject]@{{ok=$false;not_found=$false;method='spotify_app_search';error=$_.Exception.Message}} | ConvertTo-Json -Compress }}
"""
        return _ps(script, timeout=12)

    if system == "darwin":
        try:
            check = subprocess.run(["open", "-Ra", "Spotify"], capture_output=True, text=True, timeout=8)
            if check.returncode != 0:
                return {"ok": False, "not_found": True, "method": "spotify_app_search",
                        "error": check.stderr.strip() or "Spotify desktop app was not found."}
            proc = subprocess.run(["open", app_uri], capture_output=True, text=True, timeout=10)
            return {"ok": proc.returncode == 0, "not_found": False, "method": "spotify_app_search",
                    "opened": app_uri, "launch_requested": proc.returncode == 0,
                    "playback_requested": False,
                    "error": proc.stderr.strip() if proc.returncode else None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "not_found": True, "method": "spotify_app_search", "error": str(exc)}

    if system == "linux":
        # Prefer an installed Spotify executable. A registered URI handler is
        # also accepted because Flatpak/Snap installs may not expose a binary.
        xdg_open = shutil.which("xdg-open")
        spotify_bin = shutil.which("spotify")
        handler = None
        if xdg_open:
            try:
                check = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/spotify"],
                                       capture_output=True, text=True, timeout=5)
                if check.returncode == 0 and check.stdout.strip():
                    handler = check.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not spotify_bin and not handler:
            return {"ok": False, "not_found": True, "method": "spotify_app_search",
                    "error": "Spotify desktop app or URI handler was not found."}
        if spotify_bin:
            try:
                proc = subprocess.Popen([spotify_bin, app_uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"ok": True, "method": "spotify_app_search", "opened": app_uri,
                        "launch_requested": True, "playback_requested": False, "pid": proc.pid}
            except OSError as exc:
                return {"ok": False, "not_found": False, "method": "spotify_app_search", "error": str(exc)}
        try:
            proc = subprocess.run([xdg_open, app_uri], capture_output=True, text=True, timeout=10)
            return {"ok": proc.returncode == 0, "method": "spotify_app_search", "opened": app_uri,
                    "launch_requested": proc.returncode == 0, "playback_requested": False,
                    "error": proc.stderr.strip() if proc.returncode else None}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "not_found": False, "method": "spotify_app_search", "error": str(exc)}

    return {"ok": False, "not_found": True, "method": "spotify_app_search",
            "error": f"Spotify app launch is not supported on {platform.system()}."}



def _spotify_desktop_ui_play(song: str):
    """Best-effort native Spotify quick-search playback on Windows.

    Spotify documents Ctrl+K as opening Quick Search; entering a query and
    choosing a result starts that selection. We only send these keys after
    finding and activating a window whose title contains Spotify. Otherwise
    report failure so the caller can use the public web-search fallback.
    """
    if platform.system().lower() != "windows":
        return None
    try:
        import pyautogui
    except Exception as exc:
        return {"ok": False, "method": "spotify_desktop_ui", "error":
                f"Desktop keyboard automation is unavailable ({type(exc).__name__})."}

    window = None
    deadline = time.monotonic() + 7.0
    last_error = None
    while time.monotonic() < deadline:
        try:
            windows = pyautogui.getWindowsWithTitle("Spotify")
            if windows:
                window = windows[0]
                break
        except Exception as exc:
            last_error = exc
            break
        time.sleep(0.25)

    if window is None:
        detail = f" ({type(last_error).__name__})" if last_error else ""
        return {"ok": False, "method": "spotify_desktop_ui",
                "error": f"Spotify window was not found or could not be inspected{detail}."}

    try:
        if getattr(window, "isMinimized", False):
            window.restore()
        window.activate()
        time.sleep(0.35)
        # Spotify's documented quick-search shortcut. Enter on the first
        # selected quick-search result requests playback of that result.
        pyautogui.hotkey("ctrl", "k")
        time.sleep(0.25)
        pyautogui.write(song, interval=0.025)
        time.sleep(0.8)
        # First Enter submits the query. Give Spotify time to render results,
        # then move keyboard focus into the results and explicitly activate the
        # first focused result; Space is an additional play/pause request for
        # clients where Enter only opens the result page.
        pyautogui.press("enter")
        time.sleep(1.6)
        pyautogui.press("tab")
        pyautogui.press("enter")
        time.sleep(0.35)
        pyautogui.press("space")
        return {
            "ok": True,
            "action": "play_spotify",
            "method": "spotify_desktop_ui",
            "query": song,
            "playback_requested": True,
            "playback_confirmed": False,
            "note": "EVA submitted the Spotify search, then attempted to activate the focused result and request playback. Spotify sign-in, UI layout, or device state may still prevent audio from starting.",
        }
    except Exception as exc:
        return {"ok": False, "method": "spotify_desktop_ui",
                "error": f"Spotify keyboard playback could not be requested ({type(exc).__name__}: {exc})."}

def _play_spotify(song_name: str):
    """Search through the installed Spotify app or its ordinary public website.

    This path intentionally does not use Spotify Web API credentials or any
    external API. It can open/search the app or website, but cannot promise
    automatic playback when UI control or account state prevents it.
    """
    song = song_name.strip().strip("\"'")
    if not song:
        return {"ok": False, "action": "play_spotify", "error": "Please include a song or artist name."}

    native_result = _launch_spotify_app_search(song)
    if native_result and native_result.get("ok"):
        ui_result = _spotify_desktop_ui_play(song)
        if ui_result and ui_result.get("ok"):
            ui_result["app_launch"] = {
                "method": native_result.get("method", "spotify_app_search"),
                "opened": native_result.get("opened", f"spotify:search:{quote(song, safe='')}"),
            }
            return ui_result
        result = {**native_result, "action": "play_spotify", "query": song,
                  "playback_requested": False,
                  "note": "Opened the Spotify app search. Automatic playback could not be verified without successful UI control; select the desired track manually."}
        if ui_result and ui_result.get("error"):
            result["ui_playback_warning"] = ui_result["error"]
        return result

    # Normal public web search, not an API. Playback may require sign-in and
    # user selection in Spotify Web.
    url = "https://open.spotify.com/search/" + quote_plus(song)
    browser_result = _open_browser_url(url)
    result = {**browser_result, "action": "play_spotify", "query": song,
              "method": "browser_search_fallback", "playback_requested": False,
              "note": "Spotify app search was unavailable, so EVA opened Spotify Web search. Playback may require sign-in and selecting a track."}
    if native_result and native_result.get("error"):
        result["native_app_warning"] = native_result["error"]
    return result


def execute(action: str, value: str = "", *, target: str | None = None):
    """Execute one allow-listed desktop action and return a structured result."""
    system = platform.system().lower()
    if action == "notepad_new_document":
        return _new_notepad_document()
    if action == "type" and system == "windows":
        if target:
            if target.strip().casefold() not in {"notepad", "windows notepad"}:
                return {"ok": False, "error_code": "TARGET_NOT_SUPPORTED",
                        "error": "Targeted text entry currently supports Notepad only."}
            focus = _focus_notepad_window()
            if not focus.get("ok"):
                return focus
        else:
            foreground = _foreground_window_details()
            terminal_processes = {
                "windowsterminal.exe", "openconsole.exe",
                "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
            }
            terminal_title = re.search(r"\b(?:windows terminal|command prompt|powershell)\b",
                                       foreground["title"], re.I)
            if foreground["process"] in terminal_processes or terminal_title:
                return {"ok": False, "verified": True, "error_code": "TERMINAL_FOCUS_BLOCKED",
                        "error": "Text was not pasted because a terminal is focused. Ask EVA to open the intended app and retry."}
    if action == "create_file":
        return _create_file(value)
    if action == "play_youtube":
        return _play_youtube(value)
    if action == "play_spotify":
        return _play_spotify(value)
    if action == "app_open" and system == "windows":
        folder_names = {
            "downloads", "downloads folder", "documents", "documents folder",
            "desktop", "desktop folder", "pictures", "pictures folder",
            "music", "music folder", "videos", "videos folder",
        }
        app_key = value.strip().lower()
        # Ignore ordinary terminal punctuation from natural-language commands.
        # This is deliberately only for app/folder aliases, not arbitrary paths.
        app_key = re.sub(r"[\s.,!?;:]+$", "", app_key)
        if app_key not in folder_names:
            native_app_names = {
                "spotify", "whatsapp", "instagram", "facebook", "youtube", "notepad",
                "camera", "calculator", "paint", "ms paint", "chrome", "google chrome", "edge", "microsoft edge",
                "firefox", "vlc", "discord", "telegram", "zoom", "slack", "steam",
                "vs code", "vscode", "visual studio code", "microsoft teams", "teams",
            }
            native_result = _try_open_native_app(app_key)
            if native_result is not None and native_result.get("ok"):
                return native_result
            aliases = _website_aliases()
            if app_key in native_app_names:
                # A few apps have explicit, curated web clients (e.g. Spotify Web
                # and vscode.dev). These are deterministic official destinations,
                # not searches and never the Microsoft Store. Do not apply this to
                # native-only apps such as Notepad or Calculator.
                web_fallback_apps = {
                    "spotify", "whatsapp", "instagram", "facebook", "youtube",
                    "chrome", "edge", "firefox", "discord", "telegram", "zoom",
                    "slack", "steam", "vs code", "vscode", "visual studio code",
                    "teams", "microsoft teams",
                }
                if app_key in web_fallback_apps and app_key in aliases:
                    return _open_browser_url(aliases[app_key])
                # Opening a native-only app is not permission to search for it or
                # open Store. If no installed executable/app registration was found,
                # report that capability gap directly.
                return native_result or {
                    "ok": False, "error_code": "APP_NOT_INSTALLED",
                    "error": f"Could not find a launchable installed copy of '{app_key}'. No browser or Microsoft Store fallback was attempted.",
                    "launch_requested": False, "target": app_key,
                }
            # Known website aliases may use the default browser, but an unknown
            # desktop target must not silently become a search query or invoke a
            # Windows protocol handler that could open Microsoft Store.
            if app_key in aliases:
                return _open_browser_url(aliases[app_key])
            return {"ok": False, "error_code": "TARGET_NOT_REGISTERED",
                    "error": f"'{value.strip()}' is not a registered desktop application or known website. No browser search or Microsoft Store fallback was attempted.",
                    "launch_requested": False, "target": value.strip()}
    if action == "web":
        # For known desktop apps, first request a native app launch. If the app
        # is absent or its launch fails, explicitly open its website in a new tab.
        aliases = _website_aliases()
        key = value.strip().lower()
        native_error = None
        if key in aliases:
            native_result = _try_open_native_app(key)
            if native_result is not None and native_result.get("ok"):
                return native_result
            if native_result is not None:
                native_error = native_result.get("error")
            value = aliases[key]
        if system == "windows":
            try:
                url = _normalise_web_target(value)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            result = _open_browser_url(url)
            if native_error:
                result["native_app_warning"] = native_error
            return result
    if system == "darwin":
        if not MAC_PC.exists():
            return {"ok": False, "error": "Bundled macOS automation CLI is missing."}
        cmd_map = {
            "doctor": ["doctor"], "battery": ["battery"], "apps": ["apps"],
            "system_info": ["doctor"], "network_info": ["ip"],
            "wifi_status": ["doctor"], "brightness_get": ["brightness", "get"],
            "brightness_set": ["brightness", "set", value],
            "brightness_up": ["brightness", "up"], "brightness_down": ["brightness", "down"],
            "audio_devices": ["doctor"],
            "mic_test": ["mic-test"], "speaker_test": ["speaker-test"],
            "volume_get": ["volume", "get"], "volume_set": ["volume", "set", value], "volume_up": ["volume", "up"],
            "volume_down": ["volume", "down"], "mute": ["volume", "mute"],
            "media_play_pause": ["media", "play-pause"], "media_next": ["media", "next"],
            "media_prev": ["media", "prev"], "app_front": ["app", "front"],
            "app_open": ["app", "open", value], "app_quit": ["app", "quit", value],
            "screenshot": ["screenshot", value] if value else ["screenshot"],
            "clipboard_get": ["clipboard", "get"], "clipboard_set": ["clipboard", "set", value],
            "type": ["type", value], "web": ["web", value], "notify": ["notify", value],
            "say": ["say", value], "keys": ["keys", value],
            "lock_screen": ["keys", "ctrl+cmd+q"],
        }
        args = cmd_map.get(action)
        if not args:
            return {"ok": False, "error": f"Unsupported macOS automation action: {action}"}
        try:
            p = subprocess.run([sys.executable, str(MAC_PC), *args, "--json"],
                               capture_output=True, text=True, timeout=30)
            try:
                result = json.loads(p.stdout)
                if isinstance(result, dict):
                    result.setdefault("ok", p.returncode == 0)
                    return result
            except json.JSONDecodeError:
                pass
            return {"ok": p.returncode == 0, "result": p.stdout.strip(),
                    "error": p.stderr.strip() if p.returncode else ""}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
    if system == "windows":
        result = _windows(action, value)
        if action == "type" and target and isinstance(result, dict) and result.get("ok"):
            readback = _verify_notepad_text(value)
            result["readback"] = {key: val for key, val in readback.items() if key != "readable"}
            if readback.get("verified"):
                result["verified"] = True
                result["message"] = str(readback.get("message", "Notepad text was verified."))
            elif readback.get("readable"):
                result.update({"ok": False, "verified": False, "error_code": "TEXT_ENTRY_READBACK_MISMATCH",
                               "error": str(readback.get("message", "Notepad text did not match the requested content."))})
            else:
                result["verified"] = False
                result["message"] = str(readback.get("message", result.get("message", "Typing was not independently verified.")))
        return result
    return {"ok": False, "error": f"Desktop automation is not implemented for {platform.system()}. Supported: Windows and macOS."}



def _website_aliases():
    """Load the curated website alias list bundled with EVA."""
    path = AUTOMATION_DIR / "website_aliases.json"
    defaults = {
        "youtube": "https://www.youtube.com",
        "spotify": "https://open.spotify.com",
        "github": "https://github.com",
        "instagram": "https://www.instagram.com",
        "facebook": "https://www.facebook.com",
        "whatsapp": "https://web.whatsapp.com",
        "discord": "https://discord.com/app",
        "telegram": "https://web.telegram.org",
        "zoom": "https://zoom.us",
        "slack": "https://slack.com",
        "steam": "https://store.steampowered.com",
        "teams": "https://teams.microsoft.com",
        "google": "https://www.google.com",
        "wikipedia": "https://www.wikipedia.org",
        "google drive": "https://drive.google.com",
        "drive": "https://drive.google.com",
        "gmail": "https://mail.google.com",
        # VS Code is an installed desktop editor when present; vscode.dev is
        # the supported browser fallback when it is not installed.
        "vs code": "https://vscode.dev",
        "vscode": "https://vscode.dev",
        "visual studio code": "https://vscode.dev",
    }
    try:
        data = {str(k).lower().strip(): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        data = {}
    # Keep the user's curated alias file, while filling common apps that are
    # absent from older versions. These URLs are also safe browser fallbacks.
    for key, url in defaults.items():
        data.setdefault(key, url)
    # Prefer the actual Spotify player URL instead of the marketing homepage.
    data["spotify"] = "https://open.spotify.com"
    data["whatsapp"] = "https://web.whatsapp.com"
    return data


def _resolve_website_targets(fragment: str):
    """Resolve a list of known websites with multiword names and common typos."""
    aliases = {
        "yt": "youtube", "youtube": "youtube", "you tube": "youtube", "youtub": "youtube", "youtbe": "youtube", "yotube": "youtube", "youtueb": "youtube", "youtubee": "youtube",
        "git hub": "github", "githb": "github", "insta": "instagram", "instagramm": "instagram", "ig": "instagram",
        "fb": "facebook", "face book": "facebook", "whats app": "whatsapp", "whatsap": "whatsapp",
        "whatsupp": "whatsapp", "watsapp": "whatsapp", "whatapp": "whatsapp", "whatsup": "whatsapp",
        "spoti": "spotify", "spotfy": "spotify",
        "drive": "google drive", "docs": "google docs", "x": "twitter",
        "chat gpt": "chatgpt", "linked in": "linkedin", "tik tok": "tiktok",
        "disney+": "disney plus", "hbo": "hbo max",
    }
    known = _website_aliases()
    for key in known:
        aliases.setdefault(key, key)
    # Ignore harmless connective/descriptive words, but don't silently discard unknown targets.
    # Strip ordinary sentence-final punctuation from a conversational target.
    fragment = re.sub(r"[\s.,!?;:]+$", "", fragment)
    fragment = re.sub(r"\b(?:please|the|app|application|website|websites|site|sites|web|desktop|and|then|also|tabs?)\b", " ", fragment, flags=re.I)
    fragment = fragment.replace(",", " ").replace("&", " ")
    words = re.findall(r"[a-z0-9.+-]+", fragment.lower())
    if not words:
        return []
    found = []
    i = 0
    while i < len(words):
        matched = None
        # Prefer longest alias (up to 4 tokens), so "google drive" stays one target.
        for width in range(min(4, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i+width])
            if phrase in aliases:
                matched = aliases[phrase]
                i += width
                break
        if matched is None:
            return []
        found.append(matched)
    # Preserve order and avoid opening the same destination repeatedly.
    return list(dict.fromkeys(found))


def _resolve_mixed_open_targets(fragment: str):
    """Resolve ordered combinations of installed apps and websites."""
    app_aliases = {
        "notepad": "notepad", "note pad": "notepad",
        "camera": "camera", "windows camera": "camera",
        "calculator": "calculator", "calc": "calculator",
        "paint": "paint", "ms paint": "paint",
        "vs code": "vs code", "vscode": "vs code", "visual studio code": "vs code",
        "whatsapp": "whatsapp", "spotify": "spotify", "chrome": "chrome",
        "edge": "edge", "firefox": "firefox", "vlc": "vlc",
        "telegram": "telegram", "github": "github",
    }
    # Website aliases are destinations that can also try a native app first.
    aliases = {key: key for key in _website_aliases()}
    aliases.update(app_aliases)
    aliases.update({
        "yt": "youtube", "you tube": "youtube", "youtub": "youtube", "youtbe": "youtube",
        "yotube": "youtube", "youtueb": "youtube", "youtubee": "youtube",
        "git hub": "github", "githb": "github", "insta": "instagram", "ig": "instagram",
        "fb": "facebook", "face book": "facebook", "drive": "google drive",
        "whatsap": "whatsapp", "whatsupp": "whatsapp", "watsapp": "whatsapp", "whatapp": "whatsapp", "whats app": "whatsapp",
    })
    # Sentence-final punctuation must not become part of an app/site token.
    fragment = re.sub(r"[\s.,!?;:]+$", "", fragment)
    fragment = re.sub(r"\b(?:please|the|apps?|applications?|in\s+this\s+order|in\s+order|order|website|websites|site|sites|web|desktop|and|then|also|tabs?|open|launch|start|run|go\s+to|visit|two|three|four|five|six|seven|eight|nine|ten|following|these|those|several|multiple)\b", " ", fragment, flags=re.I)
    fragment = fragment.replace(",", " ").replace("&", " ")
    words = re.findall(r"[a-z0-9.+-]+", fragment.lower())
    found = []
    filler = {"in", "this", "order", "apps", "app", "applications", "application", "open", "launch", "start", "run", "go", "to", "please", "and", "then", "also", "with", "all", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "the", "these", "those", "following", "several", "multiple", "named", "called", "like", "such", "as", "first", "second", "third", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
    i = 0
    while i < len(words):
        matched = None
        for width in range(min(4, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i+width])
            if phrase in aliases:
                matched = aliases[phrase]
                i += width
                break
        if matched is not None:
            found.append(matched)
        elif words[i] in filler:
            i += 1
        else:
            return []
    return list(dict.fromkeys(found))


def _multi_action(text: str):
    """Execute each recognized step in a compound desktop request.

    Parsing is deterministic and every requested step receives its own result.
    Unknown clauses are reported rather than silently dropped.
    """
    s = text.strip()
    if not s:
        return None

    # Split only when a conjunction introduces a new imperative, not between
    # a song title and its destination ("play Baby on YouTube and Spotify").
    clauses = [c.strip(" ,") for c in re.split(
        r"\s+(?:(?:and\s+then|and|then|after that|also)\s+)(?=(?:open|launch|start|play|find|search for|increase|raise|lower|decrease|mute|unmute|take|capture|show|list|check|lock|speak|say|type|read|scroll|refresh|close|switch|zoom|go back|go forward|create)\b)",
        s, flags=re.I
    ) if c.strip(" ,")]
    if not clauses:
        return None

    def execute_open_targets(fragment, command):
        targets = _resolve_mixed_open_targets(fragment)
        if not targets:
            return None
        items = []
        native_only = {"notepad", "camera", "calculator", "vs code", "chrome", "edge", "firefox", "vlc"}
        for target in targets:
            # Apps are checked locally first. Website-capable apps fall back to a new browser tab.
            r = execute("app_open", target) if target in native_only else execute("web", target)
            items.append({**r, "command": command, "action": "open_target", "target": target,
                          "target_type": "application" if target in native_only else "website_or_app"})
        return items

    # Determine whether this is genuinely a compound action. A single site open
    # and a single supported action are handled by the normal dispatcher.
    open_match = re.fullmatch(r"(?:please\s+)?(?:open|launch|start)\s+(.+)", s, re.I)
    open_targets = _resolve_mixed_open_targets(open_match.group(1)) if open_match else []
    song_match = re.fullmatch(r"(?:please\s+)?(?:play|find|search for)\s+(.+?)\s+(?:on|in)\s+(.+)", s, re.I)
    song_targets = []
    if song_match:
        service_words = re.sub(r"\b(?:please|the|app|website|site)\b", " ", song_match.group(2), flags=re.I)
        service_words = re.sub(r"\s*(?:,|\band\b|\bor\b)\s*", " ", service_words, flags=re.I)
        song_targets = [x for x in re.findall(r"[a-z]+", service_words.lower()) if x in {"youtube", "spotify"}]
        song_targets = list(dict.fromkeys(song_targets))

    if len(clauses) <= 1 and len(open_targets) <= 1 and len(song_targets) <= 1:
        return None

    result_items = []
    last_media_query: str | None = None
    last_opened_target: str | None = None
    for clause in clauses:
        # Song searches on YouTube/Spotify. This opens results; it does not
        # claim playback started because login and user interaction may be needed.
        m = re.fullmatch(r"(?:please\s+)?(?:play|find|search for)\s+(.+?)\s+(?:on|in)\s+(.+)", clause, re.I)
        if m:
            raw_title = m.group(1).strip().strip('"\'')
            is_same_ref = bool(re.match(r"^(?:the\s+same(?:\s+(?:song|music|track|query|video|one|thing))?|that(?:\s+(?:song|music|track|one))?|same)$", raw_title, re.I))
            if is_same_ref:
                if last_media_query:
                    title = last_media_query
                else:
                    result_items.append({"command": clause, "ok": False,
                                         "error": f"Ambiguous reference '{raw_title}': no previous media query was found in earlier clauses."})
                    continue
            else:
                title = raw_title
                last_media_query = title

            tail = re.sub(r"\b(?:please|the|app|website|site)\b", " ", m.group(2), flags=re.I)
            raw_targets = re.split(r"\s*(?:,|\band\b|\bor\b)\s*", tail, flags=re.I)
            targets = [x.strip().lower() for x in raw_targets if x.strip()]
            targets = [x for x in targets if x in {"youtube", "spotify"}]
            if title and targets:
                for target in dict.fromkeys(targets):
                    last_opened_target = target
                    if target == "youtube":
                        r = execute("play_youtube", title)
                        result_items.append({**r, "command": clause, "action": "play_youtube",
                                             "target": target, "query": title})
                    else:
                        r = execute("play_spotify", title)
                        result_items.append({**r, "command": clause, "action": "play_spotify",
                                             "target": target, "query": title})
                continue

        # Open multiple sites, including aliases/misspellings and names without commas.
        m = re.fullmatch(r"(?:please\s+)?(?:open|launch|start)\s+(.+)", clause, re.I)
        if m:
            targets = _resolve_mixed_open_targets(m.group(1))
            if targets:
                if len(targets) == 1 and len(clauses) > 1:
                    r = route_and_execute(clause)
                    result_items.append({"command": clause, **r})
                else:
                    result_items.extend(execute_open_targets(m.group(1), clause) or [])
                continue
            # A non-website application (e.g. Notepad) uses the regular dispatcher.
            r = route_and_execute(clause)
            result_items.append({"command": clause, **r})
            continue

        # Any other supported single action is dispatched normally.
        if len(clauses) > 1:
            r = route_and_execute(clause)
            result_items.append({"command": clause, **r})
            continue

        # Explicit failure makes missing steps visible to the user.
        result_items.append({"command": clause, "ok": False,
                             "error": "Unrecognized step in multi-action request; no action was run for this step."})

    if len(result_items) > 1:
        return {
            "ok": all(item.get("ok", False) for item in result_items),
            "action": "multi_action",
            "results": result_items,
            "note": "Every recognized step was attempted and has an individual result. Song requests open search pages; playback may require sign-in or a user click.",
        }
    return None


def route_and_execute(text: str):
    """Map a natural-language request to a safe, explicit automation action."""
    s = text.strip()
    low = s.lower()
    # Once a request starts with an explicit type/write command, keep the entire
    # remainder literal. Drafts commonly span lines and can contain words like
    # "open" or "close" that must not be reinterpreted as desktop commands.
    freeform_type = re.fullmatch(r"(?:please\s+)?(?:type|write)\s+([\s\S]+)", s, re.I)
    if freeform_type:
        result = execute("type", freeform_type.group(1))
        result["action"] = "type"
        return result
    # Accept the handler name as a user-facing diagnostic command rather than
    # letting it fall through to ordinary chat.
    if re.fullmatch(r"/?(?:desktop[_ -]?automation|automation status|check automation|automation help|what can you control)[.!]?", low):
        result = execute("doctor")
        result["action"] = "automation_status"
        result.setdefault("commands", [
            "open YouTube / Spotify / GitHub / WhatsApp",
            "play <song> on YouTube or Spotify",
            "open <site1> <site2> <site3>",
            "system info", "network status", "show battery",
            "take a screenshot", "list apps", "increase volume",
        ])
        return result
    youtube_search = re.fullmatch(
        r"(?:please\s+)?(?:open|launch|start)\s+(?:the\s+)?youtube(?:\s+(?:app|website))?\s+and\s+search(?:\s+for)?\s+(.+)",
        s, re.I,
    )
    if youtube_search:
        query = youtube_search.group(1).strip().strip("\"'")
        url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
        result = _open_browser_url(url)
        return {**result, "action": "youtube_search", "query": query,
                "url": url, "playback_requested": False}
    multi = _multi_action(s)
    if multi is not None:
        return multi
    direct_open = re.fullmatch(r"(?:please\s+)?(?:open|launch|start)\s+(.+)", s, re.I)
    if direct_open:
        targets = _resolve_mixed_open_targets(direct_open.group(1))
        if len(targets) == 1:
            native_only = {"notepad", "camera", "calculator", "vs code", "chrome", "edge", "firefox", "vlc"}
            target = targets[0]
            result = execute("app_open", target) if target in native_only else execute("web", target)
            result["action"] = "open_target"
            result["target"] = target
            return result
    # A single song request is a first-class action, not a generic desktop command.
    song_match = re.fullmatch(r"(?:please\s+)?(?:play|find|search for)\s+(.+?)\s+(?:on|in)\s+youtube(?:\s+(?:app|website))?", s, re.I)
    if song_match:
        return _play_youtube(song_match.group(1))
    spotify_match = re.fullmatch(r"(?:please\s+)?(?:play|find|search for)\s+(.+?)\s+(?:on|in)\s+spotify(?:\s+(?:app|desktop app|website))?", s, re.I)
    if spotify_match:
        return _play_spotify(spotify_match.group(1))
    patterns = [
        (r"\b(?:create|make|new)\s+(?:a\s+)?(?:python|java|text|html|css|javascript|js|json|xml|csv|markdown|yaml|yml|pdf|word|excel|powerpoint)\s+file\b", "create_file", ""),
        (r"\bopen new tab\b", "keys", "ctrl+t"),
        (r"\bclose (?:this )?tab\b", "keys", "ctrl+w"),
        (r"\b(?:refresh|reload) (?:the )?(?:page|website|tab)\b", "keys", "ctrl+r"),
        (r"\b(?:switch to )?next tab\b", "keys", "ctrl+tab"),
        (r"\b(?:switch to )?previous tab\b", "keys", "ctrl+shift+tab"),
        (r"\bopen (?:browser )?history\b", "keys", "ctrl+h"),
        (r"\bopen bookmarks\b", "keys", "ctrl+d"),
        (r"\bgo back\b", "keys", "alt+left"),
        (r"\bgo forward\b", "keys", "alt+right"),
        (r"\b(?:open )?(?:developer tools|dev tools)\b", "keys", "ctrl+shift+i"),
        (r"\b(?:toggle )?(?:full screen|fullscreen)\b", "keys", "f11"),
        (r"\b(?:zoom in|increase browser zoom)\b", "keys", "ctrl+plus"),
        (r"\b(?:zoom out|decrease browser zoom)\b", "keys", "ctrl+-"),
        (r"\bopen browser menu\b", "keys", "alt+f"),
        (r"\bopen private (?:window|browsing)\b", "keys", "ctrl+shift+n"),
        (r"\b(?:pause|play|toggle) (?:the )?(?:video|player)\b", "keys", "space"),
        (r"\b(?:video )?fullscreen\b", "keys", "f"),
        (r"\bseek forward\b", "keys", "right"),
        (r"\bseek backward\b", "keys", "left"),
        (r"\b(?:scroll up|upar scroll karo)\b", "keys", "up"),
        (r"\b(?:scroll down|neeche scroll karo)\b", "keys", "down"),
        (r"\bscroll to top\b", "keys", "home"),
        (r"\bscroll to bottom\b", "keys", "end"),
        (r"\b(?:seek|skip) forward 10 seconds\b", "keys", "l"),
        (r"\b(?:seek|skip) backward 10 seconds\b", "keys", "j"),
        (r"\b(?:increase|raise) playback speed\b", "keys", "shift+."),
        (r"\b(?:decrease|lower) playback speed\b", "keys", "shift+,"),
        (r"\bnext video\b", "keys", "shift+n"),
        (r"\bprevious video\b", "keys", "shift+p"),
        (r"\b(?:doctor|automation status|check automation|what can you control)\b", "doctor", ""),
        (r"\b(?:system info|system information|computer specs|pc specs|hardware info)\b", "system_info", ""),
        (r"\b(?:network info|network status|show ip address|local ip|ip address)\b", "network_info", ""),
        (r"\b(?:wifi status|wi-fi status|what wifi|connected wifi)\b", "wifi_status", ""),
        (r"\b(?:audio devices|sound devices|check audio devices)\b", "audio_devices", ""),
        (r"\b(?:test|check)\s+(?:the\s+)?microphone\b|\bmic test\b", "mic_test", ""),
        (r"\b(?:test|check)\s+(?:the\s+)?speakers?\b|\bspeaker test\b", "speaker_test", ""),
        (r"\b(?:lock (?:the |my )?(?:computer|pc|screen|workstation))\b", "lock_screen", ""),
        (r"\b(?:brightness|screen brightness)\b.*\b(?:up|increase|brighter|higher)\b|\b(?:increase|raise)\b.*\bbrightness\b", "brightness_up", ""),
        (r"\b(?:brightness|screen brightness)\b.*\b(?:down|decrease|dimmer|lower)\b|\b(?:decrease|lower)\b.*\bbrightness\b", "brightness_down", ""),
        (r"\b(?:set|change)\b.{0,15}\bbrightness\b.{0,10}\b\d{1,3}\b", "brightness_set", ""),
        (r"\b(?:brightness|screen brightness)\b.*\b(?:status|level|current)\b", "brightness_get", ""),
        (r"\b(?:battery|battery percentage|battery level|charge level)\b", "battery", ""),
        (r"\b(?:list|show|what are)\b.*\b(?:running )?(?:apps|applications|processes)\b", "apps", ""),
        (r"\b(?:which app|what app).*\b(?:front|active|open)\b", "app_front", ""),
        (r"\b(?:mute|silence)(?: the)?(?: system)?(?: sound| volume| audio)?\b", "mute", ""),
        (r"\b(?:volume|sound)\b.*\b(?:up|increase|higher|louder)\b|\b(?:increase|raise)\b.*\bvolume\b", "volume_up", ""),
        (r"\b(?:volume|sound)\b.*\b(?:down|decrease|lower|quieter)\b|\b(?:decrease|lower)\b.*\bvolume\b", "volume_down", ""),
        (r"\b(?:set|change)\b.{0,15}\bvolume\b.{0,10}\b\d{1,3}\b", "volume_set", ""),
        (r"\b(?:volume|sound)\b.*\b(?:status|level|current)\b", "volume_get", ""),
        (r"\b(?:play|pause|toggle)\b.*\b(?:music|media|song|audio|playback)\b|\bplay pause\b", "media_play_pause", ""),
        (r"\b(?:next|skip)\b.*\b(?:song|track|music)\b", "media_next", ""),
        (r"\b(?:previous|prev|last)\b.*\b(?:song|track|music)\b", "media_prev", ""),
        (r"\b(?:take|capture|make)\b.*\b(?:a )?screenshot\b|\bscreenshot\b", "screenshot", ""),
        (r"\b(?:read|get|show)\b.*\bclipboard\b", "clipboard_get", ""),
        (r"\b(?:copy|put|set)\b.*\bclipboard\b", "clipboard_set", ""),
        (r"\b(?:notify|notification|show a message)\b", "notify", ""),
        (r"\b(?:say aloud|speak|read aloud)\b", "say", ""),
        (r"\b(?:open|launch|start)\b.*\b(?:website|site|url|browser|youtube|google|github|chatgpt|gmail|reddit|spotify|netflix)\b", "web", ""),
        (r"\b(?:open|launch|start)\s+(?:the\s+)?(?:app(?:lication)?\s+)?(.+)$", "app_open", ""),
        (r"\b(?:quit|close|kill|stop)\s+(?:the\s+)?(?:app(?:lication)?\s+)?(.+)$", "app_quit", ""),
        (r"\b(?:press|send)\s+(?:the\s+)?keys?\s+(.+)$", "keys", ""),
    ]
    for pattern, action, default_value in patterns:
        m = re.search(pattern, low, re.I)
        if m:
            value = default_value or ""
            if action == "web":
                value = re.sub(r"^(please\s+)?(open|launch|start)\s+", "", s, flags=re.I)
                value = re.sub(r"^(the\s+)?(website|site)\s+", "", value, flags=re.I)
            elif action in ("app_open", "app_quit"):
                value = m.group(1).strip().strip('"\'')
                value = re.sub(r"^(please\s+)?", "", value, flags=re.I)
            elif action == "volume_set":
                match_value = re.search(r"\b(\d{1,3})\b", s)
                value = match_value.group(1) if match_value else ""
            elif action in ("brightness_set",):
                number = re.search(r"\b(\d{1,3})\b", s)
                value = number.group(1) if number else ""
            elif action == "type":
                value = re.sub(r"^(please\s+)?(type|write)\s+", "", s, flags=re.I)
            elif action == "clipboard_set":
                value = re.sub(r"^(please\s+)?(copy|put|set)\s+", "", s, flags=re.I)
                value = re.sub(r"^(this\s+)?(?:text\s+)?(?:to\s+)?(?:the\s+)?clipboard\s*", "", value, flags=re.I).strip()
            elif action == "create_file":
                value = s
            elif action == "screenshot":
                value = ""
            elif action == "notify":
                value = re.sub(r"^(please\s+)?(?:notify|notification|show a message)\s*", "", s, flags=re.I).strip()
            elif action == "say":
                value = re.sub(r"^(please\s+)?(?:say aloud|speak|read aloud)\s*", "", s, flags=re.I).strip()
            elif action == "keys" and not default_value:
                value = m.group(1).strip()
            result = execute(action, value)
            result["action"] = action
            return result
    return {"ok": False, "error": "I couldn't map that to a supported desktop action. Try 'system info', 'network status', 'show battery', 'brightness up', 'open YouTube', 'take a screenshot', 'list apps', or 'type hello'."}
