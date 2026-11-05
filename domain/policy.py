"""Offline artist policy. Only a trusted Host adapter may construct authority inputs.

Scope is a typed value, not proof of Host verification. Never construct it or a
Delegation from model arguments. Stage 1 deliberately has no automatic grants.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
import json


class StoreError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError("INVALID_ARGUMENT", f"{field} must be a nonempty string")
    return value


@dataclass(frozen=True)
class Scope:
    platform: str
    stream_id: str
    user_id: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            nonempty(value, name)

    def snapshot(self) -> str:
        return canonical_json(asdict(self))


@dataclass(frozen=True)
class Delegation:
    source_ref: str
    granted_by: str
    continuous: bool

    def __post_init__(self):
        nonempty(self.source_ref, "source_ref")
        nonempty(self.granted_by, "granted_by")
        if self.continuous is not True:
            raise StoreError("AUTH_REQUIRED", "Explicit continuous delegation is required")


@dataclass(frozen=True)
class Limits:
    think: int = 0
    render: int = 0
    share: int = 0
    max_inflight: int = 0
    max_revisions: int = 0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise StoreError("INVALID_ARGUMENT", f"{name} must be a nonnegative integer")


ACTIONS = {"think": "think", "submit": "render", "edit": "render", "share": "share"}
HELD = ("reserved", "submitting", "accepted", "unknown")
CONSUMED = ("succeeded", "failed")
HONG_KONG = timezone(timedelta(hours=8))


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise StoreError("INVALID_ARGUMENT", "Parameters must be finite JSON values") from exc


def parameters(value: Dict[str, Any]) -> str:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StoreError("INVALID_ARGUMENT", "params must be a JSON object")
    # Authority and routing belong to the frozen space, never to a model payload.
    forbidden = {"scope", "owner", "platform", "stream_id", "user_id", "binding_id", "request_id"}
    if forbidden.intersection(value):
        raise StoreError("INVALID_ARGUMENT", "params cannot override identity, binding or request_id")
    return canonical_json(value)


def budget_day(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise StoreError("INVALID_CLOCK", "clock must return a timezone-aware datetime")
    return now.astimezone(HONG_KONG).date().isoformat()
