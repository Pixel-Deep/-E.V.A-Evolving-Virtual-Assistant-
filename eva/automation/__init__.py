"""EVA deterministic automation integrations and workflow engine.

Import ``get_automation_service`` for programmatic access. The service keeps
legacy desktop aliases available while registering the typed automation tools.
"""

from .service import AutomationService, close_automation_service, get_automation_service, handle_automation_request

__all__ = [
    "AutomationService", "close_automation_service", "get_automation_service",
    "handle_automation_request",
]
