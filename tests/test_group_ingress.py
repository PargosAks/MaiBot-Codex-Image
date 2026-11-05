"""Synthetic trusted-reader contract tests; no production Host acceptance claimed."""

from dataclasses import FrozenInstanceError, asdict, replace
import asyncio

import pytest

from image_plugin_under_test.inspiration.group_ingress import (
    CURRENT_HOST_SUPPORTED,
    HOST_LIMITATION,
    ConfirmedGroupMessage,
    GroupBinding,
    GroupIngress,
    GroupIngressCandidate,
    HostGroupReceipt,
)


BINDING = GroupBinding("synthetic", "real-group-session", "group-a")
CANDIDATE = GroupIngressCandidate(BINDING, "same-platform-message", "host-live-ingress-a")
NOW = 1000.0
ENABLED = 900.0
RECEIPT = HostGroupReceipt(
    binding=BINDING,
    message_id=CANDIDATE.message_id,
    sender_id="member-not-delegator",
    text="今晚雨好大\n便利店还亮着灯",
    received_at=990.0,
    receipt_id=CANDIDATE.receipt_id,
    committed_at=991.0,
    filter_passed=True,
    normal_stored=True,
    is_command=False,
    is_notify=False,
    is_self=False,
    content_kind="text",
)


class Reader:
    """Only this injected fake stands in for a future authenticated Host API."""

    def __init__(self, result=RECEIPT):
        self.result = result
        self.queries = []

    async def __call__(self, candidate):
        self.queries.append(candidate)
        return self.result


async def confirm(reader, candidate=CANDIDATE, **kwargs):
    ingress = GroupIngress(reader, clock=lambda: NOW)
    return await ingress.confirm(candidate, binding=BINDING, enabled_since=ENABLED, **kwargs)


async def test_current_host_is_unavailable_even_if_early_hook_claims_success():
    assert CURRENT_HOST_SUPPORTED is False
    assert HOST_LIMITATION == "HOST_FILTER_RECEIPT_UNAVAILABLE"
    early_hook = {
        **asdict(RECEIPT), "filtered": False, "filter_passed": True,
        "normal_stored": True, "receipt_id": "forged-from-text",
    }
    ingress = GroupIngress()
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED) is None
    assert await ingress.confirm(early_hook, binding=BINDING, enabled_since=ENABLED) is None


async def test_confirmed_result_uses_only_final_host_fields_and_preserves_text():
    reader = Reader()
    result = await confirm(reader)
    assert result == ConfirmedGroupMessage(
        BINDING, CANDIDATE.message_id, RECEIPT.sender_id, RECEIPT.text,
        RECEIPT.received_at, CANDIDATE.receipt_id,
    )
    assert result.source_key == (*asdict(BINDING).values(), CANDIDATE.message_id)
    assert reader.queries == [CANDIDATE]
    with pytest.raises(FrozenInstanceError):
        result.sender_id = "delegator"


@pytest.mark.parametrize("result", [None, {}, asdict(RECEIPT), True, "normal_stored"])
async def test_absent_pending_or_untyped_evidence_is_not_confirmation(result):
    assert await confirm(Reader(result)) is None


@pytest.mark.parametrize("changes", [
    {"binding": replace(BINDING, platform="foreign")},
    {"binding": replace(BINDING, stream_id="another-session")},
    {"binding": replace(BINDING, group_id="group-b")},
    {"message_id": "another-message"},
    {"receipt_id": "another-live-ingress"},
    {"sender_id": ""},
    {"sender_id": " member "},
    {"sender_id": True},
    {"filter_passed": False},
    {"filter_passed": 1},
    {"normal_stored": False},
    {"normal_stored": "true"},
    {"is_command": True},  # Intercepted, denied and continuing commands.
    {"is_command": 0},
    {"is_notify": True},
    {"is_notify": None},
    {"is_self": True},
    {"is_self": 0},
    {"content_kind": "attachment"},
    {"content_kind": "mixed"},
    {"content_kind": "forward"},
    {"content_kind": "reply"},
    {"text": None},
    {"text": " \n\t"},
    {"text": "x" * 4001},
])
async def test_wrong_scope_or_nonordinary_final_state_is_rejected(changes):
    assert await confirm(Reader(replace(RECEIPT, **changes))) is None


@pytest.mark.parametrize("changes", [
    {"received_at": ENABLED - 1},  # History before enablement.
    {"received_at": ENABLED},
    {"received_at": 879},  # Expired even under an earlier enable boundary.
    {"received_at": NOW + 1, "committed_at": NOW + 2},
    {"committed_at": NOW + 1},
    {"committed_at": RECEIPT.received_at - 1},
    {"received_at": float("nan")},
    {"received_at": float("inf")},
    {"received_at": "990"},
    {"received_at": True},
    {"received_at": 10 ** 1000},
    {"received_at": -1},
    {"committed_at": None},
    {"committed_at": float("nan")},
    {"committed_at": False},
])
async def test_history_future_uncommitted_and_invalid_host_times_are_rejected(changes):
    assert await confirm(Reader(replace(RECEIPT, **changes))) is None


@pytest.mark.parametrize("candidate", [
    None,
    asdict(CANDIDATE),
    replace(CANDIDATE, binding=replace(BINDING, group_id="foreign")),
    replace(CANDIDATE, message_id=""),
    replace(CANDIDATE, message_id=" padded "),
    replace(CANDIDATE, receipt_id=""),
    replace(CANDIDATE, receipt_id=True),
    replace(CANDIDATE, receipt_id="x" * 257),
])
async def test_invalid_candidate_never_triggers_host_query(candidate):
    reader = Reader()
    assert await confirm(reader, candidate) is None
    assert reader.queries == []


@pytest.mark.parametrize("enabled_since", [None, True, "900", float("nan"), -1, NOW + 1, 10 ** 1000])
async def test_invalid_enable_boundary_never_queries_host(enabled_since):
    reader = Reader()
    ingress = GroupIngress(reader, clock=lambda: NOW)
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=enabled_since) is None
    assert reader.queries == []


async def test_same_message_id_in_two_groups_never_merges_source_or_receipt():
    binding_b = replace(BINDING, group_id="group-b", stream_id="session-b")
    candidate_b = replace(CANDIDATE, binding=binding_b, receipt_id="host-live-ingress-b")
    receipt_b = replace(RECEIPT, binding=binding_b, receipt_id=candidate_b.receipt_id)
    reader = Reader(RECEIPT)
    ingress = GroupIngress(reader, clock=lambda: NOW)
    a = await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED)
    assert await ingress.confirm(candidate_b, binding=binding_b, enabled_since=ENABLED) is None
    reader.result = receipt_b
    b = await ingress.confirm(candidate_b, binding=binding_b, enabled_since=ENABLED)
    assert a.message_id == b.message_id
    assert a.source_key != b.source_key


async def test_replay_is_read_only_and_reenable_rejects_old_receipt():
    reader = Reader()
    ingress = GroupIngress(reader, clock=lambda: NOW)
    a = await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED)
    b = await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED)
    assert a == b  # Collector owns durable deduplication; confirmation has no writes.
    assert reader.result == RECEIPT
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=995) is None


async def test_query_delay_can_expire_evidence_and_exact_age_boundary_is_accepted():
    clock = iter([NOW, NOW + 111])
    ingress = GroupIngress(Reader(), clock=lambda: next(clock))
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED) is None
    ingress = GroupIngress(Reader(), clock=lambda: NOW, max_age_seconds=10)
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED) is not None


@pytest.mark.parametrize("now", [None, True, float("nan"), float("inf"), -1, 10 ** 1000])
async def test_invalid_clock_never_queries_host(now):
    reader = Reader()
    ingress = GroupIngress(reader, clock=lambda: now)
    assert await ingress.confirm(CANDIDATE, binding=BINDING, enabled_since=ENABLED) is None
    assert reader.queries == []


@pytest.mark.parametrize("binding", [
    None, GroupBinding("", BINDING.stream_id, BINDING.group_id),
    GroupBinding(BINDING.platform, "", BINDING.group_id),
    GroupBinding(BINDING.platform, BINDING.stream_id, ""),
])
async def test_invalid_binding_never_queries_host(binding):
    reader = Reader()
    ingress = GroupIngress(reader, clock=lambda: NOW)
    assert await ingress.confirm(CANDIDATE, binding=binding, enabled_since=ENABLED) is None
    assert reader.queries == []


@pytest.mark.parametrize("exception", [RuntimeError("Host query denied"), asyncio.CancelledError()])
async def test_query_failures_and_cancellation_propagate_without_retry(exception):
    calls = []

    async def reader(candidate):
        calls.append(candidate)
        raise exception

    with pytest.raises(type(exception)):
        await confirm(reader)
    assert calls == [CANDIDATE]


@pytest.mark.parametrize("kwargs", [
    {"max_age_seconds": 0}, {"max_age_seconds": float("inf")},
    {"max_age_seconds": True}, {"max_text_chars": 0}, {"max_text_chars": True},
])
def test_invalid_limits_are_explicit_configuration_errors(kwargs):
    with pytest.raises(ValueError):
        GroupIngress(**kwargs)
