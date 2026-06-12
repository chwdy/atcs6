"""Shared input validation helpers for the service layer.

Geometry/config fields edited through the entity editors (block / switch /
platform) share one rule: a value, when present and non-null, must be a
number >= 0. The UI enters lengths and radii as whole millimetres and
converts to centimetres before sending, so the backend only sees the cm
value and validates its sign and type.
"""

from __future__ import annotations

from typing import Any

from services.exceptions import ValidationError


def coerce_nonneg(value: Any, field: str) -> float:
    """Validate a non-negative number for a geometry/config field.

    ``None`` is rejected here — callers that allow "no value" (a radius/speed
    with no limit) must handle ``None`` before calling. Raises
    ``ValidationError`` on a non-numeric or negative value so the route
    handler maps it to HTTP 400.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number")
    if n < 0:
        raise ValidationError(f"{field} must be 0 or greater")
    return n
