"""Main entry point for EVA."""
from __future__ import annotations

import json
import sys

from eva.automation.engine.audit import redact
from eva.automation.service import (
    WORKFLOW_DIR,
    close_automation_service,
    get_automation_service,
)
from eva.core.orchestrator import EVAOrchestrator


def show_banner() -> None:
    print()
    print("=" * 44)
    print("       EVA - Evolving Virtual Assistant")
    print("=" * 44)
    print("  Local AI | Conversation Memory | CLI")
    print("  Type /help to see available commands.")
    print("=" * 44)
    print()


def show_help() -> None:
    print("""
EVA COMMANDS

  /help                         Show available commands
  /status                       Show current session information
  /automation                   Check desktop automation capabilities
  /tools [search]               List tools grouped by category; optionally filter by name/category
  /tool NAME                    Show parameters, permissions, dependencies, limits, and example
  /workflows                    List installed deterministic workflows
  /run-workflow NAME [JSON]     Run an installed workflow with optional JSON inputs
  /run-tool NAME [JSON]         Execute one registered tool with JSON inputs
  /reset                        Clear conversation memory
  /exit                         Close EVA

Direct automation examples:
  list files in Downloads
  find files named *.pdf in Documents
  create folder named research
  inspect webpage https://example.com
  copy file "Documents/source.txt" to "Documents/copy.txt"
  move file "Documents/old.txt" to "Documents/new.txt"  (requires approval)
  type hello world  (requires approval; acts in the focused application)

Free-form typing and explicit generic key sequences require an `approve action` confirmation on the next request because they act on whichever application has focus. Keyboard shortcuts mapped from specific safe commands (for example, `open new tab`) remain direct.

For /run-workflow inputs, pass a JSON object, for example:
  /run-workflow find_pdfs {"path": "Documents"}

Anything else you type is routed through EVA's existing task router.
""")


def _show_tools(query: str = "", detailed: bool = False) -> None:
    service = get_automation_service()
    specs = service._tool_metadata()
    query = query.strip().lower()
    if query:
        specs = [spec for spec in specs if query in spec["name"].lower()
                 or query in spec["category"].lower()
                 or query in spec["description"].lower()
                 or any(query in str(dep).lower() for dep in spec.get("dependencies", []))]
    grouped: dict[str, list[dict]] = {}
    for spec in specs:
        grouped.setdefault(spec["category"], []).append(spec)
    print("\n--- REGISTERED AUTOMATION TOOLS ---")
    if not specs:
        print("No registered tools matched that search. Use /tools to show all tools.")
    for category in sorted(grouped):
        print(f"\n[{category}]")
        for spec in grouped[category]:
            perms = ", ".join(spec.get("permissions", [])) or "none"
            print(f"  {spec['name']}: {spec['description']}")
            print(f"    permissions: {perms}; risk: {spec['risk']}; approval: {spec['requires_approval']}; timeout: {spec['timeout_seconds']}s")
            if detailed:
                print(f"    parameters: {json.dumps(spec.get('input_schema', {}), ensure_ascii=False)}")
                print(f"    dependencies: {'; '.join(spec.get('dependencies', []))}")
                print(f"    example: {spec.get('example', '')}")
    print(f"\nTotal: {len(specs)} registered tool(s).\n")


def _show_tool_help(name: str) -> None:
    service = get_automation_service()
    items = [item for item in service._tool_metadata() if item["name"] == name.strip().lower()]
    if not items:
        print(f"\nEVA: Tool '{name}' is not registered. Use /tools to list available tools.\n")
        return
    item = items[0]
    print(f"\n{item['name']} — {item['description']}")
    print(f"Category: {item['category']} | Permissions: {', '.join(item['permissions']) or 'none'}")
    print(f"Risk: {item['risk']} | Approval required by spec: {item['requires_approval']} | Timeout: {item['timeout_seconds']}s")
    print("Parameters: " + json.dumps(item['input_schema'], ensure_ascii=False, indent=2))
    print("Dependencies: " + "; ".join(item.get('dependencies', [])))
    print("Example: " + item.get('example', ''))
    print("Limitations: execution still depends on the host OS, configured approved roots, required permissions, and live service availability.\n")


def _show_workflows() -> None:
    names = sorted(
        {p.stem for p in WORKFLOW_DIR.glob("*.yaml")}
        | {p.stem for p in WORKFLOW_DIR.glob("*.yml")}
        | {p.stem for p in WORKFLOW_DIR.glob("*.json")}
    ) if WORKFLOW_DIR.exists() else []
    print("\n--- INSTALLED WORKFLOWS ---")
    print("\n".join(f"  {name}" for name in names) if names else "  No workflow files installed.")
    print()


def _run_workflow(command_text: str) -> None:
    try:
        pieces = command_text.strip().split(maxsplit=2)
        if len(pieces) < 2:
            raise ValueError("Usage: /run-workflow NAME [JSON_INPUTS]")
        inputs = json.loads(pieces[2]) if len(pieces) == 3 else {}
        if not isinstance(inputs, dict):
            raise ValueError('Workflow inputs must be a JSON object, e.g. {"path": "Documents"}.')
        service = get_automation_service()
        preview = service.preview_workflow(pieces[1], inputs)
        specs = {item["name"]: item for item in service.list_tools()}
        approval_tools = sorted({step["tool"] for step in preview["steps"]
                                 if step["tool"] in specs and (specs[step["tool"]].get("requires_approval", False)
                                 or specs[step["tool"]].get("risk") in {"high", "critical"})})
        print("\n--- WORKFLOW PREVIEW (no actions executed) ---")
        print(json.dumps(redact(preview), ensure_ascii=False, indent=2))
        approved = set()
        if approval_tools:
            answer = input(f"This workflow includes approval-gated tools: {', '.join(approval_tools)}. Type APPROVE WORKFLOW {pieces[1]} to execute: ").strip()
            if answer != f"APPROVE WORKFLOW {pieces[1]}":
                print("EVA: Workflow cancelled; no actions were executed.\n")
                return
            approved.update(approval_tools)
        result = service.execute_workflow(pieces[1], inputs, approved_actions=approved)
        payload = result.as_dict() if hasattr(result, "as_dict") else result
        print("\nEVA workflow result:\n" + json.dumps(redact(payload), ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        print(f"\nEVA: Workflow could not run: {error}\n")


def _run_tool(command_text: str) -> None:
    """Execute a registered deterministic tool; high-impact tools need CLI approval."""
    try:
        pieces = command_text.strip().split(maxsplit=2)
        if len(pieces) < 2:
            raise ValueError("Usage: /run-tool TOOL_NAME [JSON_INPUTS]")
        name = pieces[1].strip().lower()
        arguments = json.loads(pieces[2]) if len(pieces) == 3 else {}
        if not isinstance(arguments, dict):
            raise ValueError('Tool inputs must be a JSON object, e.g. {"path": "Documents"}.')
        service = get_automation_service()
        specs = {item["name"]: item for item in service.list_tools()}
        if name not in specs:
            raise ValueError(f"Tool '{name}' is not registered. Use /tools to list capabilities.")
        preview = service.execute_tool(name, arguments, dry_run=True)
        if not preview.ok:
            print("\nEVA tool validation failed:\n" + json.dumps(preview.as_dict(), ensure_ascii=False, indent=2) + "\n")
            return
        print("\n--- TOOL PREVIEW (no action executed) ---")
        print(json.dumps({"tool": name, "risk": specs[name]["risk"], "permissions": specs[name]["permissions"],
                          "arguments": redact(preview.data.get("arguments", {}))}, ensure_ascii=False, indent=2))
        risk_requires_approval = specs[name].get("requires_approval", False) or specs[name].get("risk") in {"high", "critical"}
        approved = set()
        if risk_requires_approval:
            answer = input(f"Type APPROVE {name} to execute this exact tool call, or anything else to cancel: ").strip()
            if answer != f"APPROVE {name}":
                print("EVA: Cancelled; no action was executed.\n")
                return
            approved.add(name)
        result = service.execute_tool(name, arguments, approved_actions=approved)
        print("\nEVA tool result:\n" + json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        print(f"\nEVA: Tool could not run: {error}\n")


def main() -> None:
    # Keep the CLI alive on Windows consoles whose legacy code page cannot
    # encode generated Unicode punctuation or emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    show_banner()
    try:
        eva = EVAOrchestrator()
        health = eva.check_health(timeout=1.5)
        if health.get("ok"):
            print(f"  [AI Engine] Ollama online ({health.get('model')})")
        elif health.get("reachable"):
            print(f"  [Notice] Ollama reachable at {health.get('endpoint')}, but model '{health.get('model')}' not found.")
            print(f"           Install with: ollama pull {health.get('model')}")
        else:
            print(f"  [Notice] Local Ollama server is offline at {health.get('endpoint', 'http://localhost:11434')}.")
            print("           Start with 'ollama serve' to enable conversational and writing capabilities.")
        print()
    except Exception as error:
        print(f"[Startup error] {error}")
        close_automation_service()
        return

    print("EVA: I'm ready. What's on your mind?\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue
                command = user_input.lower()

                if command == "/help":
                    show_help()
                    continue
                if command == "/status":
                    status = eva.get_status()
                    print("\n--- EVA STATUS ---")
                    print(f"Model: {status['model']}")
                    print(f"Stored messages: {status['stored_messages']}")
                    print(f"Online intelligence: {'Enabled' if status['online_enabled'] else 'Disabled'}")
                    print("------------------\n")
                    continue
                if command == "/automation":
                    print(f"\nEVA: {eva.respond('automation status')}\n")
                    continue
                if command == "/tools" or command.startswith("/tools "):
                    parts = user_input.split(maxsplit=1)
                    query = parts[1] if len(parts) > 1 else ""
                    _show_tools(query=query, detailed=False)
                    continue
                if command.startswith("/tool "):
                    _show_tool_help(user_input.split(maxsplit=1)[1])
                    continue
                if command == "/workflows":
                    _show_workflows()
                    continue
                if command.startswith("/run-workflow "):
                    _run_workflow(user_input)
                    continue
                if command == "/run-tool" or command.startswith("/run-tool "):
                    _run_tool(user_input)
                    continue
                if command == "/reset":
                    eva.reset_conversation()
                    print("EVA: Fresh start. Our conversation is cleared.\n")
                    continue
                if command in {"/exit", "/quit"}:
                    print("EVA: See you later! 👋")
                    break

                reply = eva.respond(user_input)
                print(f"\nEVA: {reply}\n")
            except (KeyboardInterrupt, EOFError):
                print("\nEVA: Shutting down. See you soon! 👋")
                break
            except Exception as error:
                print(f"\n[Error] Something went wrong: {error}\n")
    finally:
        close_automation_service()


if __name__ == "__main__":
    main()
