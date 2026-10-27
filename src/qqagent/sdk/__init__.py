"""Public SDK used by extensions running inside the extension host."""

from .api import (
    CapabilitiesAPI,
    ExtensionAPI,
    ExtensionLogger,
    HookRegistration,
    HooksAPI,
    PromptFragment,
    PromptsAPI,
    StorageAPI,
    ToolContext,
    ToolRegistration,
    ToolsAPI,
)

__all__ = [
    "CapabilitiesAPI",
    "ExtensionAPI",
    "ExtensionLogger",
    "HookRegistration",
    "HooksAPI",
    "PromptFragment",
    "PromptsAPI",
    "StorageAPI",
    "ToolContext",
    "ToolRegistration",
    "ToolsAPI",
]
