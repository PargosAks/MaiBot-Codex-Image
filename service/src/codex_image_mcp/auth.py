"""Signed envelopes bind the transport credential to trusted host identity and arguments."""

import base64
import hashlib
import hmac
import json
import time


def unpack(
    envelope: str, secret: str, operation: str, allowed_users: tuple[str, ...]
) -> dict:
    try:
        payload, signature = envelope.split(".")
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(raw)
        now = time.time()
        scope = data["scope"]
        if data["op"] != operation or abs(now - data["iat"]) > 120:
            raise ValueError
        if set(scope) != {"platform", "stream_id", "user_id"}:
            raise ValueError
        if not all(isinstance(v, str) and v.strip() for v in scope.values()):
            raise ValueError
        if f"{scope['platform']}:{scope['user_id']}" not in allowed_users:
            raise ValueError
        if not isinstance(data["args"], dict):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Unauthorized request context") from exc


def owner_key(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
