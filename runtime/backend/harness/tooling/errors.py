from __future__ import annotations


class ToolingError(Exception):
    """Base error for ToolRuntime infrastructure failures."""


class DuplicateToolError(ToolingError):
    """Raised when a tool name is registered more than once."""


class ToolNotFoundError(ToolingError):
    """Raised when a requested tool is not registered."""


class ToolInputValidationError(ToolingError):
    """Raised when a tool call input does not satisfy the minimal schema."""
