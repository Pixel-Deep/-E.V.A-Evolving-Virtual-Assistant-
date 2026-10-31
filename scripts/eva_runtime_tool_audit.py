#!/usr/bin/env python3
"""Read-only EVA tool/connection audit with isolated temporary-directory smoke tests.

Default behavior:
- inspects every registered tool's schema and metadata
- preflights every bundled YAML/JSON workflow without executing it
- performs temporary-directory filesystem, CSV/JSON, ZIP, and local-time round trips
- checks local Ollama reachability and installed integration dependencies without
  running model inference, launching visible apps, or making external HTTP requests

Optional probes:
  --probe-content-generation  Ask the configured local model for one short cow paragraph
  --probe-browser             Launch a local headless browser and render a local blank page
  --probe-network             Make one GET request to https://example.com
  --probe-web-browse          Exercise EVA's integrated HTML search + browser page reader

Public-web browsing uses ordinary HTML pages rather than a search API or API key.
Ollama communication is local to the configured localhost service. No credentials
or model response bodies are written to the report. All temporary
file changes are confined to a TemporaryDirectory and removed at exit.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eva.config import OLLAMA_MODEL, OLLAMA_URL  # noqa: E402
from eva.automation.service import AutomationService  # noqa: E402
from eva.automation.engine.workflows import load_workflow  # noqa: E402


def _status(ok: bool, message: str, **details: Any) -> dict[str, Any]:
    return {"status": "PASS" if ok else "FAIL", "message": message, **details}


def _probe_ollama(timeout: float = 2.5) -> dict[str, Any]:
    url = OLLAMA_URL.rstrip("/") + "/api/tags"
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = [str(item.get("name", "")) for item in models if isinstance(item, dict) and item.get("name")]
        present = OLLAMA_MODEL in names or any(name.split(":", 1)[0] == OLLAMA_MODEL.split(":", 1)[0] for name in names)
        return {
            "status": "PASS" if present else "WARN",
            "message": "Ollama is reachable and the configured model is listed." if present else
                       "Ollama is reachable, but the configured model was not found in /api/tags.",
            "endpoint": url,
            "configured_model": OLLAMA_MODEL,
            "model_present": present,
            "available_models": names,
        }
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"status": "WARN", "message": "Ollama did not respond to the local read-only probe.",
                "endpoint": url, "configured_model": OLLAMA_MODEL,
                "error": f"{type(exc).__name__}: {exc}"[:400]}


def _probe_content_generation(timeout: float = 65.0) -> dict[str, Any]:
    """Bounded optional model smoke test; no tools, files, or desktop actions are sent."""
    prompt = (
        "Write exactly one short factual paragraph about a cow as a domestic animal. "
        "Mention milk and grass. Do not write a letter, application, email, heading, or list. Return only the paragraph."
    )
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": "Follow the requested genre and topic exactly. Do not substitute an unrelated template."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"num_predict": 180, "num_ctx": 2048, "temperature": 0.2},
    }
    request = urllib.request.Request(
        OLLAMA_URL.rstrip("/") + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
        message = body.get("message", {}) if isinstance(body, dict) else {}
        generated = str(message.get("content", "")).strip() if isinstance(message, dict) else ""
        low = generated.casefold()
        letter_like = low.startswith(("dear ", "subject:", "to whom it may concern", "respected sir", "respected madam")) or \
                      "yours faithfully" in low or "school leave application" in low
        paragraph_count = len([part for part in re.split(r"\n\s*\n", generated) if part.strip()])
        valid = (bool(generated) and ("cow" in low or "cows" in low) and not letter_like
                 and "grass" in low and "milk" in low and paragraph_count == 1)
        # Do not save or print the model's complete response; report only safe diagnostics.
        return {"status": "PASS" if valid else "FAIL",
                "message": "Configured model followed the cow-paragraph genre/topic check." if valid else
                           "Configured model failed the cow-paragraph output contract; EVA should not type this output.",
                "configured_model": OLLAMA_MODEL, "elapsed_seconds": round(time.monotonic() - started, 2),
                "nonempty": bool(generated), "mentions_cow": "cow" in low or "cows" in low,
                "mentions_milk": "milk" in low, "mentions_grass": "grass" in low,
                "letter_like": letter_like, "paragraph_count": paragraph_count}
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"status": "FAIL", "message": "Local content-generation probe failed.",
                "configured_model": OLLAMA_MODEL, "error": f"{type(exc).__name__}: {exc}"[:400],
                "elapsed_seconds": round(time.monotonic() - started, 2)}


def _probe_browser() -> dict[str, Any]:
    # Exercise EVA's real browser wrapper, including its fallback to an installed
    # system browser, rather than probing Playwright independently of EVA.
    session = None
    try:
        from eva.automation.tools.browser import BrowserSession
        session = BrowserSession(headless=True, timeout_ms=8_000)
        result = session.run_serialized("local_smoke_test")
        ok = bool(result.get("ok"))
        return _status(ok, "EVA browser launch, local page render, and semantic target check succeeded." if ok else
                       "EVA browser launched but a local page/semantic target check failed.",
                       browser_version=result.get("browser_version"),
                       browser_executable=result.get("browser_executable"),
                       content_verified=result.get("content_verified"),
                       semantic_target_verified=result.get("semantic_target_verified"))
    except Exception as exc:  # browser executable, driver, dependencies, or runtime may be missing
        return {"status": "FAIL", "message": "EVA headless browser probe failed.",
                "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if session is not None:
            session.close()


def _probe_network(timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request("https://example.com", headers={"User-Agent": "EVA-runtime-audit/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(response.status)
            final_host = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(response.geturl()).hostname
        ok = 200 <= status_code < 400
        return _status(ok, "External HTTPS probe completed." if ok else "External HTTPS probe returned an error status.",
                       status_code=status_code, final_host=final_host)
    except Exception as exc:
        return {"status": "FAIL", "message": "External HTTPS probe failed.",
                "error": f"{type(exc).__name__}: {exc}"[:400]}


def _probe_web_browse(timeout: float = 85.0) -> dict[str, Any]:
    """Exercise the actual integrated public HTML search + browser reading tool.

    This intentionally uses no search-provider SDK or API key. It is an opt-in
    probe because it makes public HTTPS requests and launches headless Chromium.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="eva-web-browse-audit-") as td:
        temp_root = Path(td).resolve()
        service = AutomationService(
            allowed_roots=[temp_root], audit_path=None,
            workflows_dir=PROJECT_ROOT / "config" / "workflows",
            capability_gaps_path=temp_root / "capability_gaps.json",
            browser_headless=True,
        )
        try:
            result = service.execute_tool("web.browse", {
                "query": "official Python downloads python.org",
                "max_pages": 1,
                "max_chars_per_page": 1500,
            })
            ok = bool(
                result and result.ok and result.verified and isinstance(result.data, dict)
                and int(result.data.get("page_count", 0)) >= 1
                and bool(str(result.data.get("research_text", "")).strip())
                and bool(result.data.get("pages"))
                and bool(str(result.data["pages"][0].get("url", "")).startswith(("https://", "http://")))
            )
            if not ok:
                return {
                    "status": "FAIL",
                    "message": "Integrated public-web browse did not verify a readable public page.",
                    "tool_ok": bool(result and result.ok),
                    "tool_verified": bool(result and result.verified),
                    "error_code": getattr(result, "error_code", None),
                    "tool_message": str(getattr(result, "message", "No tool result."))[:400],
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            page = result.data["pages"][0]
            return {
                "status": "PASS",
                "message": "EVA's API-free HTML search + browser page-reading path fetched and verified a readable public page.",
                "source_mode": result.data.get("source_mode"),
                "page_count": int(result.data.get("page_count", 0)),
                "first_page_title": str(page.get("title", ""))[:200],
                "first_page_url": str(page.get("url", ""))[:500],
                "first_page_text_chars": len(str(page.get("text", ""))),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "uses_external_search_api": False,
            }
        except Exception as exc:
            return {
                "status": "FAIL",
                "message": "Integrated public-web browse probe raised an error.",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
        finally:
            service.close()


def _validate_tool_catalog(service: AutomationService) -> dict[str, Any]:
    public_tools = service.list_tools()
    metadata = service._tool_metadata()
    names = [str(item.get("name", "")) for item in public_tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    failures: list[str] = []
    metadata_by_name = {str(item.get("name", "")): item for item in metadata}
    for tool in public_tools:
        name = str(tool.get("name", "<unnamed>"))
        schema = tool.get("input_schema", tool.get("schema", {}))
        if not isinstance(schema, dict) or schema.get("type") != "object":
            failures.append(f"{name}: missing object input schema")
            continue
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            failures.append(f"{name}: schema properties is not an object")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list) or any(field not in properties for field in required):
            failures.append(f"{name}: required fields do not match the schema properties")
        item = metadata_by_name.get(name, {})
        if not item:
            failures.append(f"{name}: absent from planner metadata")
        else:
            if not item.get("purpose"):
                failures.append(f"{name}: missing purpose")
            if not item.get("when_to_use"):
                failures.append(f"{name}: missing when_to_use guidance")
            if not item.get("output_description"):
                failures.append(f"{name}: missing output description")
            if not isinstance(item.get("dependencies"), list):
                failures.append(f"{name}: dependencies is not a list")
    if duplicates:
        failures.append("Duplicate registered tool names: " + ", ".join(duplicates))
    return {
        "status": "PASS" if not failures else "FAIL",
        "message": f"Validated {len(public_tools)} registered tool contracts.",
        "tool_count": len(public_tools), "duplicate_names": duplicates,
        "failures": failures[:100],
        "tools": [{
            "name": str(item.get("name", "")),
            "category": str(item.get("category", "")),
            "permissions": item.get("permissions", []),
            "input_schema": item.get("input_schema", item.get("schema", {})),
            "risk": item.get("risk", ""),
            "idempotent": item.get("idempotent"),
            "dependencies": metadata_by_name.get(str(item.get("name", "")), {}).get("dependencies", []),
            "when_to_use": metadata_by_name.get(str(item.get("name", "")), {}).get("when_to_use", []),
            "output_description": metadata_by_name.get(str(item.get("name", "")), {}).get("output_description", ""),
        } for item in public_tools],
    }


def _probe_workflows(service: AutomationService, workflows_dir: Path, temp_root: Path) -> dict[str, Any]:
    paths = sorted([*workflows_dir.glob("*.yaml"), *workflows_dir.glob("*.yml"), *workflows_dir.glob("*.json")])
    details = []
    for path in paths:
        try:
            workflow = load_workflow(path)
            values: dict[str, Any] = {}
            for name in workflow.get("inputs", {}):
                if name.casefold() == "url":
                    values[name] = "https://example.com"
                elif name.casefold() == "pattern":
                    values[name] = "*.txt"
                elif name.casefold() in {"path", "directory", "source", "destination", "folder"}:
                    values[name] = str(temp_root / f"{name}-sample")
                else:
                    values[name] = str(temp_root / f"{name}-sample")
            preview = service.workflows.preview(workflow, values)
            details.append({"name": path.name, "status": "PASS" if preview.get("ok") else "FAIL",
                            "message": str(preview.get("message", preview.get("error", "preflight complete")))[:300]})
        except Exception as exc:
            details.append({"name": path.name, "status": "FAIL",
                            "message": f"{type(exc).__name__}: {exc}"[:300]})
    failures = [item for item in details if item["status"] != "PASS"]
    return {"status": "PASS" if paths and not failures else "FAIL",
            "message": f"Preflighted {len(paths)} bundled workflows without running their actions.",
            "workflow_count": len(paths), "failures": failures, "workflows": details}


def _probe_local_tools(service: AutomationService, temp_root: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        if "files.create_text_file" not in service.registry.names() or "files.read_text" not in service.registry.names():
            raise RuntimeError("Required text file read/write tools are not registered.")
        path = temp_root / "roundtrip.txt"
        expected = "EVA temporary round-trip\nLine two with punctuation: 'quotes', commas, and unicode £.\n"
        created = service.execute_tool("files.create_text_file", {"path": str(path), "content": expected})
        if not created.ok:
            raise RuntimeError(f"files.create_text_file failed: {created.message}")
        read = service.execute_tool("files.read_text", {"path": str(path)})
        actual = str(read.data.get("text", "")) if read.ok else ""
        checks["filesystem_create_read_roundtrip"] = _status(read.ok and actual == expected,
            "UTF-8 text file content round-tripped exactly." if read.ok and actual == expected else
            "Text file read did not match its written content.", verified=bool(read.verified))

        # Exercise the registered office-document writer instead of treating its
        # schema as proof that a real DOCX can be created. The test stays inside
        # the temporary approved root and checks both ZIP integrity and content.
        docx_path = temp_root / "roundtrip.docx"
        docx_expected = "EVA DOCX round-trip\nSecond paragraph & punctuation."
        docx_result = service.execute_tool("files.create_docx", {
            "path": str(docx_path), "content": docx_expected
        }) if "files.create_docx" in service.registry.names() else None
        docx_ok = False
        if docx_result and docx_result.ok and docx_path.is_file() and zipfile.is_zipfile(docx_path):
            try:
                with zipfile.ZipFile(docx_path) as archive:
                    docx_ok = (archive.testzip() is None and "word/document.xml" in archive.namelist()
                               and "EVA DOCX round-trip" in archive.read("word/document.xml").decode("utf-8")
                               and "Second paragraph &amp; punctuation." in archive.read("word/document.xml").decode("utf-8"))
            except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
                docx_ok = False
        checks["docx_create_integrity_roundtrip"] = _status(
            docx_ok, "A genuine DOCX package was created and its content verified." if docx_ok else
            (docx_result.message if docx_result and not docx_result.ok else
             "DOCX creation, ZIP integrity, or content verification failed."),
            tool_registered="files.create_docx" in service.registry.names())

        # Verify every registered document writer against the matching real reader.
        # Registration/schema checks alone can miss extension-to-writer errors and
        # reader exceptions such as an extension-specific local-import regression.
        format_cases = (
            ("docx", ".docx", "files.create_docx", True),
            ("pptx", ".pptx", "files.create_pptx", True),
            ("xlsx", ".xlsx", "files.create_xlsx", True),
            ("pdf", ".pdf", "files.create_pdf", False),
            ("rtf", ".rtf", "files.create_rtf", False),
            ("odt", ".odt", "files.create_odt", True),
            ("batch_text", ".bat", "files.create_text_file", False),
        )
        for label, suffix, tool_name, is_zip_package in format_cases:
            path = temp_root / f"format-roundtrip{suffix}"
            expected_text = f"EVA {label} format round-trip phrase"
            registered = tool_name in service.registry.names() and "files.read_document" in service.registry.names()
            created = service.execute_tool(tool_name, {"path": str(path), "content": expected_text}) if registered else None
            read = service.execute_tool("files.read_document", {"path": str(path), "max_bytes": 20000}) if created and created.ok else None
            extracted = str(read.data.get("text", "")) if read and read.ok else ""
            integrity_ok = bool(created and created.ok and path.is_file() and extracted and expected_text in extracted)
            if is_zip_package and integrity_ok:
                try:
                    with zipfile.ZipFile(path) as archive:
                        integrity_ok = (archive.testzip() is None and len(archive.namelist()) > 2)
                except (OSError, zipfile.BadZipFile):
                    integrity_ok = False
            if suffix == ".pdf" and integrity_ok:
                integrity_ok = path.read_bytes().startswith(b"%PDF-")
            if suffix == ".rtf" and integrity_ok:
                integrity_ok = "Calibri" not in extracted
            checks[f"{label}_create_read_roundtrip"] = _status(
                integrity_ok,
                f"{suffix} writer created a valid file and its reader recovered the expected content." if integrity_ok else
                (created.message if created is not None and not created.ok else
                 read.message if read is not None and not read.ok else
                 f"{suffix} output or reader content/integrity validation failed."),
                writer_tool=tool_name, writer_registered=tool_name in service.registry.names(),
                reader_registered="files.read_document" in service.registry.names(),
                file_exists=path.is_file(),
            )

        # Exercise the structured-data converter with only temporary in-root files.
        csv_path = temp_root / "sample.csv"
        json_path = temp_root / "sample.json"
        csv_text = "name,amount\nTea,2\nBread,1\n"
        csv_created = service.execute_tool("files.create_text_file", {"path": str(csv_path), "content": csv_text})
        converted = service.execute_tool("data.csv_to_json", {"source": str(csv_path), "destination": str(json_path)}) if csv_created.ok else None
        json_read = service.execute_tool("files.read_text", {"path": str(json_path)}) if converted and converted.ok else None
        rows = json.loads(str(json_read.data.get("text", "[]"))) if json_read and json_read.ok else None
        ok_json = isinstance(rows, list) and len(rows) == 2 and rows[0].get("name") == "Tea" and rows[0].get("amount") == "2"
        checks["csv_to_json_roundtrip"] = _status(bool(ok_json), "CSV converted to the expected JSON rows." if ok_json else
            "CSV/JSON conversion did not produce the expected records.", tool_registered="data.csv_to_json" in service.registry.names())

        # Exercise archive create/extract integrity inside the same temporary root.
        source_dir = temp_root / "archive_source"
        service.execute_tool("files.create_directory", {"path": str(source_dir)})
        member = source_dir / "hello.txt"
        service.execute_tool("files.create_text_file", {"path": str(member), "content": "zip audit payload"})
        zip_path = temp_root / "audit.zip"
        zipped = service.execute_tool("archive.zip_create", {"source": str(source_dir), "destination": str(zip_path)})
        extracted = service.execute_tool("archive.zip_extract", {"source": str(zip_path), "destination": str(temp_root / "unzipped")}) if zipped.ok else None
        restored = temp_root / "unzipped" / "hello.txt"
        ok_zip = bool(zipped.ok and extracted and extracted.ok and restored.is_file() and restored.read_text(encoding="utf-8") == "zip audit payload")
        checks["zip_create_extract_roundtrip"] = _status(ok_zip, "ZIP create/extract content and integrity were checked." if ok_zip else
            "ZIP create/extract round-trip failed.", archive_entries=zipped.data.get("files") if zipped and zipped.ok else None)

        current_time = service.execute_tool("system.local_time", {}) if "system.local_time" in service.registry.names() else None
        checks["local_time_tool"] = _status(bool(current_time and current_time.ok and current_time.data),
            "Registered local-time tool returned structured data." if current_time and current_time.ok else
            "Local-time tool did not return structured data.")
    except Exception as exc:
        checks["local_tools_smoke"] = {"status": "FAIL", "message": f"{type(exc).__name__}: {exc}"[:500]}
    return checks


def _connection_inventory(service: AutomationService) -> dict[str, Any]:
    names = set(service.registry.names())
    module_specs = {
        "sympy": "sympy", "pyautogui": "pyautogui", "pywhatkit": "pywhatkit",
        "playwright": "playwright", "pywinauto": "pywinauto", "yaml": "yaml",
        "python-pptx": "pptx", "openpyxl": "openpyxl", "reportlab": "reportlab", "pypdf": "pypdf",
    }
    python_packages = {label: importlib.util.find_spec(module) is not None for label, module in module_specs.items()}
    binaries = {name: shutil.which(name) for name in ("git", "ollama", "powershell", "powershell.exe", "pwsh", "javac", "java")}
    binaries = {name: path for name, path in binaries.items() if path}
    email_send_tools = sorted(name for name in names if any(part in name.casefold() for part in ("email.send", "gmail.send", "outlook.send", "mail.send")))
    browser_tools = sorted(name for name in names if name.startswith("browser."))
    network_tools = sorted(name for name in names if name.startswith("web.") or name.startswith("network."))
    windows_tools = sorted(name for name in names if name.startswith("windows."))
    return {
        "os": platform.platform(), "python": sys.version.split()[0],
        "packages_available": python_packages, "binaries_found": binaries,
        "registered_groups": {
            "browser": browser_tools, "network_and_search": network_tools,
            "windows_specific": windows_tools,
            "email_send": email_send_tools,
            "content_drafting": [name for name in names if name == "content.generate_text"],
        },
        "email_send_connection": "registered" if email_send_tools else "not_registered; drafting can work but sending is not connected as a native tool",
        "public_web_browsing": "ordinary HTML search + controlled browser; no external search API or API key",
        "spotify_integration": "native app or public website fallback only; no Spotify Web API is integrated",
        "windows_ui_status": "applicable" if platform.system() == "Windows" else "not_available_on_this_host",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "work" / "EVA_tool_connection_audit.json",
                        help="Report path (default: work/EVA_tool_connection_audit.json)")
    parser.add_argument("--probe-content-generation", action="store_true",
                        help="Run one bounded local-model genre/topic compliance test")
    parser.add_argument("--probe-browser", action="store_true",
                        help="Launch a headless browser and render a local page; no external URL is visited")
    parser.add_argument("--probe-network", action="store_true",
                        help="Send one GET request to https://example.com")
    parser.add_argument("--probe-web-browse", action="store_true",
                        help="Exercise EVA's full API-free public HTML search + browser page reader")
    args = parser.parse_args()

    args.output = args.output.expanduser().resolve(strict=False)
    report: dict[str, Any] = {
        "audit_version": 1, "timestamp_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project_root": str(PROJECT_ROOT),
        "safety": "No visible app launches or changes outside a temporary directory are made by default.",
    }
    with tempfile.TemporaryDirectory(prefix="eva-runtime-audit-") as td:
        temp_root = Path(td).resolve()
        service = AutomationService(allowed_roots=[temp_root], audit_path=None,
                                    workflows_dir=PROJECT_ROOT / "config" / "workflows",
                                    capability_gaps_path=temp_root / "capability_gaps.json",
                                    browser_headless=True)
        try:
            catalog = _validate_tool_catalog(service)
            report["tool_catalog"] = catalog
            report["connections"] = _connection_inventory(service)
            report["ollama_read_only_probe"] = _probe_ollama()
            report["workflow_preflight"] = _probe_workflows(service, PROJECT_ROOT / "config" / "workflows", temp_root)
            report["local_tool_smoke"] = _probe_local_tools(service, temp_root)
        finally:
            service.close()
    if args.probe_content_generation:
        report["content_generation_probe"] = _probe_content_generation()
    else:
        report["content_generation_probe"] = {"status": "NOT_RUN", "message": "Use --probe-content-generation to test the actual configured local model."}
    if args.probe_browser:
        report["headless_browser_probe"] = _probe_browser()
    else:
        report["headless_browser_probe"] = {"status": "NOT_RUN", "message": "Use --probe-browser to launch Chromium and render a local page."}
    if args.probe_network:
        report["external_network_probe"] = _probe_network()
    else:
        report["external_network_probe"] = {"status": "NOT_RUN", "message": "External network connectivity was not tested."}
    if args.probe_web_browse:
        report["public_web_browse_probe"] = _probe_web_browse()
    else:
        report["public_web_browse_probe"] = {"status": "NOT_RUN", "message": "Use --probe-web-browse to test EVA's complete public HTML search + browser reader path."}

    required_checks = [report["tool_catalog"], report["workflow_preflight"], *report["local_tool_smoke"].values()]
    required_pass = all(item.get("status") == "PASS" for item in required_checks)
    requested_probe_keys = []
    if args.probe_content_generation:
        requested_probe_keys.append("content_generation_probe")
    if args.probe_browser:
        requested_probe_keys.append("headless_browser_probe")
    if args.probe_network:
        requested_probe_keys.append("external_network_probe")
    if args.probe_web_browse:
        requested_probe_keys.append("public_web_browse_probe")
    requested_probe_failures = [key for key in requested_probe_keys if report[key].get("status") != "PASS"]
    if not required_pass or requested_probe_failures:
        report["overall"] = "FAIL"
    else:
        warnings = []
        ollama_status = report["ollama_read_only_probe"].get("status")
        if ollama_status != "PASS":
            warnings.append("ollama_read_only_probe")
        if report["connections"].get("email_send_connection", "").startswith("not_registered"):
            warnings.append("email_send_connection_not_registered")
        not_run = [key for key in ("content_generation_probe", "headless_browser_probe", "external_network_probe", "public_web_browse_probe")
                   if report[key].get("status") == "NOT_RUN"]
        report["warnings"] = warnings
        report["optional_probes_not_run"] = not_run
        report["overall"] = "PASS_WITH_WARNINGS" if warnings else "PASS"
    if requested_probe_failures:
        report["requested_probe_failures"] = requested_probe_failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"EVA Runtime Tool Audit: {report['overall']}")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Registered tools: {report['tool_catalog']['tool_count']} ({report['tool_catalog']['status']})")
    print(f"Bundled workflows: {report['workflow_preflight']['workflow_count']} ({report['workflow_preflight']['status']})")
    for name, result in report["local_tool_smoke"].items():
        print(f"{name}: {result.get('status')} — {result.get('message', '')}")
    ollama = report["ollama_read_only_probe"]
    print(f"Ollama: {ollama['status']} — {ollama['message']}")
    print(f"Email send connection: {report['connections']['email_send_connection']}")
    for key in ("content_generation_probe", "headless_browser_probe", "external_network_probe", "public_web_browse_probe"):
        print(f"{key}: {report[key].get('status')} — {report[key].get('message', '')}")
    for warning in report.get("warnings", []):
        print(f"WARNING: {warning}")
    for failed in report.get("requested_probe_failures", []):
        print(f"REQUESTED PROBE FAILED: {failed}")
    print(f"Report: {args.output}")
    return 0 if report["overall"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
