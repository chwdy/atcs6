"""Typed exceptions raised by the service layer.

Route handlers map these to HTTP status codes:

    ValidationError         -> 400
    NotFoundError           -> 404
    ConflictError           -> 409
    GoneError               -> 410
    ServiceUnavailableError -> 503
    ServiceError            -> 500 (or treat as generic)

Non-HTTP callers (CLI, journey tests) just catch them as ordinary Python
exceptions.
"""


class ServiceError(Exception):
    """Base class for all service-layer errors."""


class ValidationError(ServiceError):
    """Request data did not pass validation. Maps to HTTP 400."""


class NotFoundError(ServiceError):
    """A requested entity does not exist. Maps to HTTP 404."""


class ConflictError(ServiceError):
    """The requested change conflicts with current state. Maps to HTTP 409."""


class GoneError(ServiceError):
    """The requested operation targets a field/endpoint removed in v1.

    Maps to HTTP 410.
    """


class ServiceUnavailableError(ServiceError):
    """A required runtime collaborator is not wired yet. Maps to HTTP 503."""
