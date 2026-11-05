"""Fixed Host execution proves observe is not post-filter admission evidence.

Reuse the public_host/SDK/dispatcher/runner fixture from test_host_integration,
but replace its permissive filter/command stubs with AST-loaded implementations
from HOST_REV. Execute the real HeartFlow receiver too. Synthetic configuration,
session lookup, command executor, persistence, memory and person services record
effects only; no Host bootstrap, database, network, model or platform is used.
The artist scheduler stays gated by host_chain. No receipt or passing flag is
invented: group_inspiration is enabled, with the actual production observe hook
and no production receipt reader or notebook admission path.
"""

from copy import deepcopy
from types import ModuleType, SimpleNamespace
import asyncio
import os
import re
import sys

import pytest
import test_host_integration as host_integration

from image_plugin_under_test.inspiration.group_notebook import DATABASE_NAME
from image_plugin_under_test.domain.policy import Scope
from test_host_integration import (
    HOST_REV,
    PLATFORM,
    STREAM,
    host_file,
    load_nodes,
)


# Export the existing fixtures for pytest without importing unrelated tests.
public_host = host_integration.public_host
host_chain = host_integration.host_chain


pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)

GROUP = "synthetic-group"
MEMBER = "synthetic-member-not-delegator"
BAN_WORD = "synthetic-banned-word"
BAN_REGEX = r"\bsynthetic-regex-\d+\b"
COMMAND = "/boundary " + BAN_WORD + " synthetic-regex-7"


@pytest.fixture
async def filter_boundary(host_chain, monkeypatch):
    h = host_chain
    host, trace = h.host, []
    ns = host.__dict__
    ns["re"] = re
    # Selection preserves each real method's AST; no filtering rule is copied.
    load_nodes(ns, host_file("common/utils/utils_message.py"), {"MessageUtils"},
               {"MessageUtils": {"check_ban_words", "check_ban_regex"}})
    methods = {
        "receive_message", "_invoke_message_hook", "_coerce_int", "_is_command_candidate",
        "_process_commands", "_mark_command_message", "_store_intercepted_command_message",
        "_handle_command_processing_result",
    }
    load_nodes(ns, host_file("chat/message_receive/bot.py"), {"ChatBot"}, {"ChatBot": methods})
    # Keep peripheral routing/notice plumbing from Harness, while restoring real
    # methods shadowed by that fixture's instance-level command stubs.
    bot = host.ChatBot()
    bot.__dict__.update(h.bot.__dict__)
    h.bot = bot
    for name in ("_is_command_candidate", "_process_commands", "_handle_command_processing_result"):
        delattr(bot, name)
    load_nodes(ns, host_file("chat/heart_flow/heartflow_message_processor.py"), {"HeartFCMessageReceiver"})
    bot.heartflow_message_receiver = host.HeartFCMessageReceiver()

    host.global_config.message_receive = SimpleNamespace(ban_words=[BAN_WORD], ban_msgs_regex=[BAN_REGEX])
    real_words, real_regex = host.MessageUtils.check_ban_words, host.MessageUtils.check_ban_regex

    def words(text):
        result = real_words(text)
        trace.append(("ban_words", result))
        return result

    def regex(text):
        result = real_regex(text)
        trace.append(("ban_regex", result))
        return result

    async def store(message):
        trace.append(("store", {"message_id": message.message_id, "is_command": message.is_command,
                                "intercept": message.message_info.additional_config.get("intercept_message_level")}))

    host.MessageUtils.check_ban_words = staticmethod(words)
    host.MessageUtils.check_ban_regex = staticmethod(regex)
    host.MessageUtils.store_message_to_db_async = store

    def register(message):
        trace.append(("register_ingress", message.message_id))

    async def session(*args, **kwargs):
        trace.append(("ensure_session", STREAM))

    host.chat_manager = SimpleNamespace(register_message=register, get_or_create_session=session)

    async def execute(**kwargs):
        trace.append(("execute_command", kwargs["message"].message_id))
        return True, None, 1

    command_info = SimpleNamespace(plugin_name="synthetic-command", name="boundary", permission="user")

    def find_command(text):
        return (execute, {}, command_info) if text == COMMAND else None

    host.component_query_service = SimpleNamespace(find_command_by_text=find_command,
                                                   get_plugin_config=lambda name: {})
    host.global_announcement_manager = SimpleNamespace(get_disabled_chat_commands=lambda stream: ())
    host.is_command_disabled = lambda command: False
    host.is_local_operator = lambda *args: False
    host.is_clear_context_command = lambda text: False
    host.CLEAR_COMMAND_ID = "core.clear"
    host.CLEAR_CONTEXT_COMMAND = "/clear"

    async def chat_register(message):
        trace.append(("register_heartflow", message.message_id))

    async def heartflow(stream):
        trace.append(("resolve_heartflow", stream))
        return SimpleNamespace(register_message=chat_register)

    async def memory(message):
        trace.append(("memory", message.message_id))

    def person(**kwargs):
        trace.append(("person", (kwargs["user_id"], kwargs["group_id"])))

    host.heartflow_manager = SimpleNamespace(get_or_create_heartflow_chat=heartflow)
    host.Person = SimpleNamespace(register_person=person)
    memory_module = ModuleType("src.services.memory_flow_service")
    memory_module.memory_automation_service = SimpleNamespace(on_incoming_message=memory)
    monkeypatch.setitem(sys.modules, memory_module.__name__, memory_module)

    original_config = h.config

    def enabled_group_config(**kwargs):
        config = original_config(**kwargs)
        config["autonomy"]["spaces"][0]["group_inspiration"] = True
        return config

    h.config = enabled_group_config
    await h.load(group=True)
    assert h.plugin.config.autonomy.spaces[0].group_inspiration is True
    assert "studio" in h.runtime.verified_notebook_groups
    return h, trace


async def receive(h, text, message_id):
    """Synthetic group envelope; all admission decisions belong to loaded Host."""
    message = SimpleNamespace(
        message_id=message_id, platform=PLATFORM, session_id=STREAM,
        message_info=SimpleNamespace(
            user_info=SimpleNamespace(user_id=MEMBER, user_nickname="synthetic", user_cardname=None),
            group_info=SimpleNamespace(group_id=GROUP, group_name="synthetic-group"), additional_config={}),
        raw_message=SimpleNamespace(components=[h.host.TextComponent(text=text)]),
        processed_plain_text=text, timestamp=h.clock(), received_at=h.clock(),
        account_id="", scope="", reply_to=None, is_mentioned=False, is_at=False,
        is_emoji=False, is_picture=False, is_command=False, is_notify=False, process=h.noop,
    )

    def unchanged(raw):
        # Command handling really mutates is_command later. Compare to the live
        # object, not an earlier snapshot, without implementing a fake admission.
        assert raw == h.host.serialize_session_message(message)
        return message

    h.host.deserialize_session_message = unchanged
    await h.bot.receive_message(message)
    if h.dispatcher._background_tasks:
        await asyncio.gather(*tuple(h.dispatcher._background_tasks))
    return message


@pytest.mark.parametrize("case,text,expected", [
    ("ordinary", "今晚雨好大，便利店还亮着灯", [
        "ban_words", "ban_regex", "register_ingress", "ensure_session", "resolve_heartflow",
        "store", "register_heartflow", "memory", "person",
    ]),
    ("ban_word", "今晚下雨 " + BAN_WORD, ["ban_words"]),
    ("ban_regex", "便利店 synthetic-regex-7", ["ban_words", "ban_regex"]),
    ("intercepted_command", COMMAND, [
        "register_ingress", "ensure_session", "execute_command", "store",
    ]),
    # Candidate lookup strips the input; execution looks up the original text.
    # Our exact command fixture models an anchored command pattern.
    ("padded_candidate", " " + COMMAND + " ", [
        "register_ingress", "ensure_session", "resolve_heartflow", "store", "register_heartflow", "memory", "person",
    ]),
])
async def test_real_host_observe_filter_and_storage_boundary(filter_boundary, caplog, case, text, expected):
    h, trace = filter_boundary
    scope = h.runtime.artists["studio"].scope
    assert scope != Scope(PLATFORM, STREAM, MEMBER)
    before_effects = deepcopy((h.calls, h.renderer.calls, h.sends, h.reviews))
    message_id = "boundary-" + case
    message = await receive(h, text, message_id)

    # The real OBSERVE dispatcher/runner delivers rejected input too. Its
    # background callback may run AFTER filtering: it still sees the earlier,
    # pre-command snapshot. Never equate callback wall time with admission.
    assert len(h.hook_calls) == 1
    observed = h.hook_calls[0]["message"]
    assert observed["processed_plain_text"] == text
    assert observed["message_id"] == message_id and observed["session_id"] == STREAM
    assert observed["platform"] == PLATFORM
    assert observed["message_info"]["group_info"]["group_id"] == GROUP
    assert observed["message_info"]["user_info"]["user_id"] == MEMBER
    assert observed["is_command"] is False

    assert [name for name, _ in trace] == expected
    if case == "ban_word":
        assert trace == [("ban_words", (True, BAN_WORD))]
    elif case == "ban_regex":
        assert trace == [("ban_words", (False, None)), ("ban_regex", (True, BAN_REGEX))]
    elif case == "ordinary":
        assert trace[:2] == [("ban_words", (False, None)), ("ban_regex", (False, None))]
        assert ("store", {"message_id": message_id, "is_command": False, "intercept": None}) in trace
        assert trace[-1] == ("person", (MEMBER, GROUP))
        assert message.is_command is False
    elif case == "padded_candidate":
        assert BAN_WORD in text
        assert message.is_command is False
        assert ("store", {"message_id": message_id, "is_command": False, "intercept": None}) in trace
        # Even a newly persisted ordinary-looking row for this exact delivery
        # could not attest that either word/regex check actually ran.
        assert not any(name in ("ban_words", "ban_regex", "execute_command") for name, _ in trace)
    else:
        # Actual Host command classification bypasses ordinary ban filters and
        # marks/stores the intercepted command without entering HeartFlow.
        assert message.is_command is True
        assert trace[-1] == ("store", {"message_id": message_id, "is_command": True, "intercept": 1})

    # Real plugin observer + enabled group collection config; no injected reader,
    # forged passing flag, synthetic receipt or notebook accept() call is used.
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not h.runtime.store.audit_operations("studio", scope)
    assert not (h.data_paths.data_dir / DATABASE_NAME).exists()
    assert (h.calls, h.renderer.calls, h.sends, h.reviews) == before_effects
    assert not h.gate.is_set()
    assert not [record for record in caplog.records if record.levelno >= 40]
    assert HOST_REV == "062d45cf8bd4272ac57d7ec03fcf405e5a458211"
