#!/usr/bin/env python3
"""Read-only one-by-one audit of EVA's registered tool contracts and routing shortlist.

No registered tool handler is executed. The audit instantiates the service against a
fresh temporary approved root, inspects each live binding/schema/permission contract,
and checks that representative compound requests expose the necessary schemas.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eva.automation.service import AutomationService  # noqa: E402
from eva.automation.task_planner import _tool_rows  # noqa: E402
from eva.core.task_router import TaskRouter  # noqa: E402

ROUTE_SCENARIOS: dict[str, tuple[str, str]] = {
    "mixed_office_and_code": ("Create an Excel expense tracker and a Python script that reads it", "desktop_automation"),
    "existing_source_static_validation": ("Validate the syntax of my Python script sample.py", "desktop_automation"),
    "new_code_generation": ("Create a Python script and validate it", "coding_engine"),
    "deterministic_math": ("What is 18 percent of 450?", "math_engine"),
    "explanation_not_execution": ("Explain this task in simple words: organize all my Downloads by file type.", "ollama"),
    "notepad_letter": ("Open Notepad and write a letter to the principal", "desktop_automation"),
    "compound_web_research": ("Research the official Python docs, summarize the installation steps, and save a Word document called setup.docx", "desktop_automation"),
    "local_date": ("What is today's date?", "system_time"),
    "compound_math_and_desktop": ("Calculate 5 + 7 and open YouTube", "desktop_automation"),
}


SCENARIOS: dict[str, tuple[str, set[str]]] = {
    "mixed_office_and_code": (
        "Create an Excel expense tracker and a Python script that reads it",
        {"files.create_xlsx", "content.generate_text", "files.create_unique_text_file", "coding.validate_file"},
    ),
    "web_research_to_word_document": (
        "Research the official Python docs, summarize the installation steps, and save a Word document called setup.docx",
        {"web.browse", "content.generate_text", "files.create_docx"},
    ),
    "gmail_spam_navigation": (
        "Open Gmail and show my spam folder",
        {"browser.navigate", "browser.snapshot", "browser.click_by_role"},
    ),
    "latest_pdf_summary_to_file": (
        "Find the latest PDF in Downloads and summarize it in a text file",
        {"files.find_latest", "files.read_document", "content.generate_text", "files.create_unique_text_file"},
    ),
    "notepad_letter_and_saved_copy": (
        "Open Notepad, write a letter to the principal, and save a copy on Desktop",
        {"desktop.open_target", "content.generate_text", "desktop.type_notepad", "files.create_unique_text_file"},
    ),
    "csv_to_json": (
        "Convert my expenses.csv to JSON",
        {"data.csv_to_json", "files.find_one"},
    ),
    "browser_named_control": (
        "Inspect the website and click the sign in button",
        {"browser.snapshot", "browser.click_by_role"},
    ),
    "deterministic_project_scaffold": (
        "Create a new Python CLI starter project named Study Tracker",
        {"coding.scaffold_project"},
    ),
    "static_code_validation": (
        "Validate the syntax of my Python script sample.py",
        {"coding.validate_file", "files.resolve_by_name"},
    ),
    "git_status": (
        "Check git status in this project",
        {"developer.git_status"},
    ),
    "compound_math_and_desktop": (
        "Calculate 5 + 7 and open YouTube",
        {"math.solve", "desktop.open_target"},
    ),
}


def schema_issues(spec: Any) -> list[str]:
    issues: list[str] = []
    schema = spec.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        issues.append("input schema is not an object schema")
        return issues
    if schema.get("additionalProperties") is not False:
        issues.append("additionalProperties must be false")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        issues.append("properties must be an object")
        return issues
    missing = sorted(set(schema.get("required", [])) - set(properties))
    if missing:
        issues.append("required keys missing from properties: " + ", ".join(missing))
    for key, rule in properties.items():
        if not isinstance(rule, dict):
            issues.append(f"parameter {key!r} has no schema object")
        elif rule.get("type") == "array" and not isinstance(rule.get("items"), dict):
            issues.append(f"array parameter {key!r} has no item schema")
    return issues


def build_report() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="eva-tool-orchestration-audit-") as td:
        root = Path(td).resolve()
        service = AutomationService(
            allowed_roots=[root], audit_path=None,
            workflows_dir=root / "empty-workflows",
            capability_gaps_path=root / "state" / "gaps.json",
            browser_headless=True,
        )
        try:
            metadata = {item["name"]: item for item in service._tool_metadata()}
            tools: list[dict[str, Any]] = []
            category_counts: dict[str, int] = defaultdict(int)
            errors: list[str] = []
            for spec in service.registry.list():
                binding = service.registry.get(spec.name)
                item = metadata.get(spec.name, {})
                issues = schema_issues(spec)
                if binding is None or not callable(binding.handler):
                    issues.append("registered tool has no callable handler")
                for field in ("purpose", "when_to_use", "output_description", "side_effects"):
                    if not item.get(field):
                        issues.append(f"missing discoverability contract: {field}")
                if spec.risk.value in {"high", "critical"} and spec.risk.value not in service.policy.approval_required_risks and not spec.requires_approval:
                    issues.append("high-risk tool has no approval gate")
                category_counts[spec.category] += 1
                if issues:
                    errors.append(f"{spec.name}: " + "; ".join(issues))
                tools.append({
                    "name": spec.name,
                    "category": spec.category,
                    "description": spec.description,
                    "purpose": item.get("purpose", ""),
                    "when_to_use": item.get("when_to_use", []),
                    "input_schema": spec.input_schema,
                    "required_parameters": spec.input_schema.get("required", []),
                    "aliases": list(spec.aliases),
                    "permissions": list(spec.permissions),
                    "risk": spec.risk.value,
                    "requires_approval": spec.requires_approval,
                    "approval_by_risk_policy": spec.risk.value in service.policy.approval_required_risks,
                    "timeout_seconds": spec.timeout_seconds,
                    "idempotent": spec.idempotent,
                    "dependencies": item.get("dependencies", []),
                    "output_description": item.get("output_description", ""),
                    "side_effects": item.get("side_effects", ""),
                    "handler_registered": bool(binding and callable(binding.handler)),
                    "contract_issues": issues,
                })

            top_level_routes: list[dict[str, Any]] = []
            router = TaskRouter()
            for label, (prompt, expected_handler) in ROUTE_SCENARIOS.items():
                decision = router.route(prompt)
                passed = decision.handler == expected_handler
                top_level_routes.append({"scenario": label, "prompt": prompt,
                                         "expected_handler": expected_handler,
                                         "actual_handler": decision.handler,
                                         "category": decision.category,
                                         "reason": decision.reason,
                                         "status": "PASS" if passed else "FAIL"})
                if not passed:
                    errors.append(f"top-level route {label}: expected {expected_handler}, got {decision.handler}")

            routing: list[dict[str, Any]] = []
            for label, (prompt, required_tools) in SCENARIOS.items():
                rows = _tool_rows(service, prompt)
                selected = {str(row.get("name")) for row in rows}
                missing = sorted(required_tools - selected)
                passed = not missing and len(rows) <= 16
                routing.append({"scenario": label, "prompt": prompt, "schema_count": len(rows),
                                "required_tools": sorted(required_tools), "selected_tools": sorted(selected),
                                "missing_required_schemas": missing, "status": "PASS" if passed else "FAIL"})
                if not passed:
                    errors.append(f"routing shortlist {label} missing schemas: {', '.join(missing)}")

            return {
                "audit": "EVA one-by-one tool and orchestration audit",
                "read_only": True,
                "tool_count": len(tools),
                "category_counts": dict(sorted(category_counts.items())),
                "tool_contract_status": "PASS" if not any(tool["contract_issues"] for tool in tools) else "FAIL",
                "top_level_route_status": "PASS" if all(item["status"] == "PASS" for item in top_level_routes) else "FAIL",
                "top_level_routes": top_level_routes,
                "routing_scenario_status": "PASS" if all(item["status"] == "PASS" for item in routing) else "FAIL",
                "routing_scenarios": routing,
                "tools": tools,
                "errors": errors,
                "environment_limits": [
                    "This audit does not execute tool handlers, launch visible applications, or access the public internet.",
                    "It does not test the user's live Ollama model or Windows focus/window behavior.",
                    "The local agent loop still depends on the model selecting the correct next tool; this audit validates schema discoverability, not semantic success on every possible prompt.",
                ],
            }
        finally:
            service.close()


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# EVA One-by-One Tool and Orchestration Audit — 2026-10-24",
        "",
        f"- Registered tools: **{report['tool_count']}**",
        f"- Tool contract status: **{report['tool_contract_status']}**",
        f"- Top-level route status: **{report['top_level_route_status']}**",
        f"- Routing shortlist status: **{report['routing_scenario_status']}**",
        "- Mode: read-only; no tool handlers were executed.",
        "",
        "## Routing shortlist regression matrix",
        "",
        "| Scenario | Schemas selected | Status | Missing required schemas |",
        "|---|---:|---|---|",
    ]
    for item in report["routing_scenarios"]:
        lines.append(f"| `{item['scenario']}` | {item['schema_count']} | {item['status']} | {', '.join(item['missing_required_schemas']) or '—'} |")
    lines.extend(["", "## Top-level intent-routing regression matrix", "",
                  "| Scenario | Expected handler | Actual handler | Status |",
                  "|---|---|---|---|"])
    for item in report["top_level_routes"]:
        lines.append(f"| `{item['scenario']}` | `{item['expected_handler']}` | `{item['actual_handler']}` | {item['status']} |")
    lines.extend(["", "## Tool-by-tool registry", ""])
    for category in sorted(report["category_counts"]):
        lines.extend([f"### {category} ({report['category_counts'][category]})", ""])
        for tool in [item for item in report["tools"] if item["category"] == category]:
            required = ", ".join(tool["required_parameters"]) or "none"
            permissions = ", ".join(tool["permissions"]) or "none"
            approval = "yes" if tool["requires_approval"] or tool["approval_by_risk_policy"] else "no"
            lines.append(f"- **`{tool['name']}`** — {tool['description']} Parameters required: `{required}`. Permissions: `{permissions}`. Risk: `{tool['risk']}`; approval gate: `{approval}`; timeout: {tool['timeout_seconds']}s.")
            lines.append(f"  - Use when: {'; '.join(tool['when_to_use']) if isinstance(tool['when_to_use'], list) else tool['when_to_use']}")
            lines.append(f"  - Returns: {tool['output_description']}")
            lines.append(f"  - Side effects: {tool['side_effects']}")
            if tool["contract_issues"]:
                lines.append(f"  - **Contract issues:** {'; '.join(tool['contract_issues'])}")
        lines.append("")
    lines.extend(["## Findings", ""])
    if report["errors"]:
        lines.extend(f"- {item}" for item in report["errors"])
    else:
        lines.append("- No static tool-contract or representative schema-shortlist failures were found.")
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {item}" for item in report["environment_limits"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path", type=Path, help="Write the full JSON audit report to this path")
    parser.add_argument("--markdown", type=Path, help="Write the detailed Markdown audit report to this path")
    parser.add_argument("--check-only", action="store_true", help="Print a concise result and exit nonzero on audit failure")
    args = parser.parse_args()
    report = build_report()
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown_report(report), encoding="utf-8")
    if args.check_only:
        print(f"Tools: {report['tool_count']} | contracts: {report['tool_contract_status']} | top-level routes: {report['top_level_route_status']} | routing shortlist: {report['routing_scenario_status']}")
        for error in report["errors"]:
            print("FAIL: " + error)
    elif not args.json_path and not args.markdown:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
