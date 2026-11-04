"""Read-only, fail-closed boundary for group inspiration evidence.

Host audit (2026-11-03): MaiBot 062d45cf8bd4272ac57d7ec03fcf405e5a458211
and installed local-maibot/tools/maibot, version 1.3.15, have matching source
(ignoring CRLF) for the following six files:

* src/chat/message_receive/bot.py:821-850: after_process precedes word/regex
  filtering. Hooks can still rewrite identity/text; an early snapshot is not final.
  Commands bypass these ordinary-message filters (839), execute at 878, and
  intercepted/permission-denied commands also store messages (312, 629-658).
* src/chat/heart_flow/heartflow_message_processor.py:60: normal store follows
  filtering/command handling; chat registration and memory automation follow it.
  There is no public immutable post-normal-store notification/receipt here.
* src/plugin_runtime/capabilities/data.py:431-454: message.get_by_id queries
  persisted messages, optionally scoped to chat_id, with no ingress verdict.
* src/plugin_runtime/host/message_utils.py:415-457: that query serializes final
  sender/group/platform/session/message/text, command/notify flags and received_at.
  These fields do not certify which path wrote the row or this live delivery.
* src/services/message_service.py:160-177: lookup is latest matching row, not a
  unique live ingress lookup; unscoped lookup can return another chat's message.
* src/plugin_runtime/host/authorization.py:44-59: non-declared capabilities are
  denied. This plugin's manifest does not grant message.get_by_id. Existing
  chat.get_stream_by_group_id verifies a binding, not message acceptance.

Consequently no production reader is supplied. Even granting message.get_by_id
alone does not create independent post-filter/live-ingress provenance. Do not
poll from after_process (the snapshot predates filtering regardless of when
the background observer runs), read MaiBot.db,
copy Host filters, or accept a flag/receipt supplied in user/model text.

Minimal proposed Host extension, NOT implemented or called by this module:
after normal store commits, issue a fresh, Host-only ingress receipt and emit an
immutable post-commit notification with its opaque receipt_id. Exclude commands
(including continuing commands), notices, self messages and non-text components.
A separately declared/authorized read-only capability must resolve that receipt
to an immutable final snapshot plus Host receive/commit times and verdicts. Bind
the receipt to the exact live delivery, platform, real session and group; never
resolve it by message_id alone or accept an adapter-supplied receipt identifier.
Receipts must expire and must not be reconstructed from historical rows. A
trusted reader for that future API, or an isolated synthetic reader, can implement
TrustedReceiptReader below. Injection is a trust boundary, not authentication by
Python type or by boolean flags.

Integration: only call while ArtistSpace.group_inspiration is true (default
false); use the current enable epoch as enabled_since and recheck authorization,
binding and enable epoch after await. This adapter has no persistence or replay
cache: the collector must deduplicate by the confirmed message's source_key,
including across restarts. Never use the contributor as the space's delegator.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Tuple
import math
import time


CURRENT_HOST_SUPPORTED = False
HOST_LIMITATION = "HOST_FILTER_RECEIPT_UNAVAILABLE"


@dataclass(frozen=True)
class GroupBinding:
    platform: str
    stream_id: str
    group_id: str


@dataclass(frozen=True)
class GroupIngressCandidate:
    """Locator from a future trusted Host post-commit notification, never text."""

    binding: GroupBinding
    message_id: str
    receipt_id: str


@dataclass(frozen=True)
class ConfirmedGroupMessage:
    binding: GroupBinding
    message_id: str
    sender_id: str
    text: str
    received_at: float
    receipt_id: str

    @property
    def source_key(self) -> Tuple[str, str, str, str]:
        return (self.binding.platform, self.binding.stream_id, self.binding.group_id, self.message_id)


@dataclass(frozen=True)
class HostGroupReceipt:
    """Authoritative snapshot; all verdicts must be explicit, not inferred.

    received_at/committed_at are finite epoch seconds stamped by Host, not the
    platform message timestamp. content_kind='text' certifies only text source
    components, not an attachment/forward/reply description rendered into text.
    """

    binding: GroupBinding
    message_id: str
    sender_id: str
    text: str
    received_at: float
    receipt_id: str
    committed_at: float
    filter_passed: bool
    normal_stored: bool
    is_command: bool
    is_notify: bool
    is_self: bool
    content_kind: str


class TrustedReceiptReader(Protocol):
    async def __call__(self, candidate: GroupIngressCandidate) -> Optional[HostGroupReceipt]:
        """Exact authenticated Host receipt query; no history scans or effects.

        Return None for absent, pending, expired or denied receipts. Transport
        failures may raise; callers must not turn failures into accepted input.
        """
        ...


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()


def _binding(value: object) -> bool:
    return isinstance(value, GroupBinding) and all(
        _identifier(item) for item in (value.platform, value.stream_id, value.group_id)
    )


def _timestamp(value: object) -> bool:
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class GroupIngress:
    """Confirm evidence without enqueueing, storing, generating or sending.

    With no reader this is deliberately unavailable on the audited Host. Reader
    exceptions/cancellation propagate; no retries or optimistic acceptance occur.
    Repeated confirmation is read-only and can return the same evidence again.
    """

    def __init__(
        self,
        reader: Optional[TrustedReceiptReader] = None,
        *,
        clock: Callable[[], float] = time.time,
        max_age_seconds: float = 120.0,
        max_text_chars: int = 4000,
    ) -> None:
        if not _timestamp(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be finite and positive")
        if type(max_text_chars) is not int or max_text_chars <= 0:
            raise ValueError("max_text_chars must be a positive integer")
        self._reader = reader
        self._clock = clock
        self._max_age_seconds = max_age_seconds
        self._max_text_chars = max_text_chars

    async def confirm(
        self,
        candidate: GroupIngressCandidate,
        *,
        binding: GroupBinding,
        enabled_since: float,
    ) -> Optional[ConfirmedGroupMessage]:
        """Return only the Host's final text/sender, or None when unconfirmed."""
        if self._reader is None:
            return None
        now = self._clock()
        if (
            not _binding(binding)
            or not isinstance(candidate, GroupIngressCandidate)
            or candidate.binding != binding
            or not _identifier(candidate.message_id)
            or not _identifier(candidate.receipt_id)
            or not _timestamp(enabled_since)
            or not _timestamp(now)
            or enabled_since > now
        ):
            return None
        receipt = await self._reader(candidate)
        # Recheck time after I/O: an evidence query may outlive the input window.
        now = self._clock()
        if not isinstance(receipt, HostGroupReceipt) or not _timestamp(now):
            return None
        if (
            receipt.binding != binding
            or receipt.message_id != candidate.message_id
            or receipt.receipt_id != candidate.receipt_id
            or not _identifier(receipt.sender_id)
            or receipt.filter_passed is not True
            or receipt.normal_stored is not True
            or receipt.is_command is not False
            or receipt.is_notify is not False
            or receipt.is_self is not False
            or receipt.content_kind != "text"
            or not isinstance(receipt.text, str)
            or not receipt.text.strip()
            or len(receipt.text) > self._max_text_chars
            or not _timestamp(receipt.received_at)
            or not _timestamp(receipt.committed_at)
        ):
            return None
        if not (
            enabled_since < receipt.received_at <= receipt.committed_at <= now
            and now - receipt.received_at <= self._max_age_seconds
        ):
            return None
        return ConfirmedGroupMessage(
            binding=receipt.binding,
            message_id=receipt.message_id,
            sender_id=receipt.sender_id,
            text=receipt.text,
            received_at=receipt.received_at,
            receipt_id=receipt.receipt_id,
        )
