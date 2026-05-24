"""Service layer between HTTP route handlers and domain entities.

Service functions take an `AppState` plus structured kwargs, return domain
objects (or raise a typed exception on validation/lookup failure). They
contain no HTTP-specific concerns -- no `request`, no `jsonify`, no Flask
status codes. Route handlers translate `ServiceError` subclasses into HTTP
responses; non-HTTP callers (CLI tools, journey tests, scripted scenarios)
can call the same functions directly.
"""

from services.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)

__all__ = [
    "ConflictError",
    "NotFoundError",
    "ServiceError",
    "ValidationError",
]
