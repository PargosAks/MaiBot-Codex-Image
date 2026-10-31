"""Current-event ingestion with synthetic images; no vision, draw or send calls."""

from io import BytesIO
from threading import Event
from types import SimpleNamespace
import asyncio
import base64
import hashlib
import json
import struct
import zlib

from PIL import Image
import pytest

from image_plugin_under_test import host_media
from image_plugin_under_test.host_runtime import ArtistSpace, HostRuntime
from image_plugin_under_test.policy import Delegation, Scope, StoreError
from image_plugin_under_test.store import ArtistStore

SCOPE = Scope("test", "media-stream", "media-owner")


def image_bytes(fmt="PNG", *, animated=False):
    output = BytesIO()
    first = Image.new("RGB", (8, 6), "red")
    options = {"save_all": True, "append_images": [Image.new("RGB", (8, 6), "blue")]} if animated else {}
    first.save(output, format=fmt, **options)
    return output.getvalue()


def component(data=None, **kwargs):
    data = image_bytes() if data is None else data
    return {"type": "image", "binary_data_base64": base64.b64encode(data).decode("ascii"), **kwargs}


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
@pytest.mark.parametrize("hash_kind", [None, "md5", "sha256", "opaque-host-hash"])
def test_actual_format_and_known_host_hashes(fmt, mime, hash_kind):
    data = image_bytes(fmt)
    host_hash = getattr(hashlib, hash_kind)(data).hexdigest().upper() if hash_kind in {"md5", "sha256"} else hash_kind
    result = host_media.current_images({"raw_message": [{"type": "text", "data": "metadata"},
                                                       component(data, hash=host_hash, mime="image/gif")]})
    assert len(result) == 1
    assert result[0].message_index == 1 and result[0].data == data and result[0].mime == mime
    assert result[0].sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("part,code", [
    ({"type": "image", "binary_data_base64": "!not-base64!"}, "MEDIA_BASE64_INVALID"),
    ({"type": "image", "binary_data_base64": "图"}, "MEDIA_BASE64_INVALID"),
    ({"type": "image", "data": "https://example.invalid/image.png"}, "MEDIA_BYTES_REQUIRED"),
    ({"type": "image", "data": "C:/private/image.png"}, "MEDIA_BYTES_REQUIRED"),
    ({"type": "image", "binary_data_base64": ""}, "MEDIA_BYTES_REQUIRED"),
    (component(b"not an image"), "MEDIA_FORMAT_INVALID"),
    (component(image_bytes("GIF")), "MEDIA_FORMAT_INVALID"),
    (component(hash="0" * 32), "MEDIA_HASH_MISMATCH"),
    (component(hash="f" * 64), "MEDIA_HASH_MISMATCH"),
])
def test_bad_bytes_or_hash_fail_without_echoing_component(part, code):
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [part]})
    assert caught.value.code == code
    assert str(caught.value) == "Current Host image input was rejected"


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_truncated_image_is_rejected(fmt):
    data = image_bytes(fmt)
    with pytest.raises(StoreError):
        host_media.current_images({"raw_message": [component(data[:len(data) // 2])]})


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_animation_is_rejected(fmt):
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [component(image_bytes(fmt, animated=True))]})
    assert caught.value.code == "MEDIA_ANIMATION_UNSUPPORTED"


def test_no_emoji_history_forward_url_or_path_loading():
    part = component()
    message = {"raw_message": [dict(part, type="emoji"), {"type": "forward", "data": [part]},
                               {"type": "reply", "data": part}, {"type": "file", "data": part}],
               "history": [part], "url": "https://example.invalid/image", "path": "C:/private/image.png"}
    assert host_media.current_images(message) == []


def test_count_and_encoded_size_limits_are_checked_before_image_decoding(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversize batch must be rejected before decoding")

    monkeypatch.setattr(host_media, "_validate_image", forbidden)
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [component()] * 4})
    assert caught.value.code == "MEDIA_COUNT_EXCEEDED"
    too_long = "A" * (4 * ((host_media.MAX_IMAGE_BYTES + 2) // 3) + 1)
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [{"type": "image", "binary_data_base64": too_long}]})
    assert caught.value.code == "MEDIA_SIZE_EXCEEDED"


def test_exact_byte_and_total_limits(monkeypatch):
    # Test boundary arithmetic independently of image compression sizes.
    data = image_bytes()
    monkeypatch.setattr(host_media, "MAX_IMAGE_BYTES", len(data))
    monkeypatch.setattr(host_media, "MAX_EVENT_BYTES", len(data) * 3)
    assert len(host_media.current_images({"raw_message": [component(data)] * 3})) == 3
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [component(data + b"x")]})
    assert caught.value.code == "MEDIA_SIZE_EXCEEDED"
    monkeypatch.setattr(host_media, "MAX_EVENT_BYTES", len(data) * 3 - 1)
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [component(data)] * 3})
    assert caught.value.code == "MEDIA_TOTAL_EXCEEDED"


def test_64_million_pixel_limit_checked_without_allocating_large_raster():
    data = image_bytes()
    ihdr = struct.pack(">IIBBBBB", 8001, 8000, 8, 2, 0, 0, 0)
    data = data[:8] + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + data[33:]
    with pytest.raises(StoreError) as caught:
        host_media.current_images({"raw_message": [component(data)]})
    assert caught.value.code == "MEDIA_PIXELS_EXCEEDED"


def test_reference_policy_defaults_do_not_expand_old_text_consent():
    settings = ArtistSpace(consent_ref="old text consent")
    assert settings.reference_inputs is False and settings.media_consent_ref == ""
    with pytest.raises(ValueError):
        ArtistSpace(reference_inputs="true")


@pytest.fixture
def runtime(tmp_path):
    store = ArtistStore(tmp_path / "media.sqlite3")
    store.create_space("studio", SCOPE, binding_id="synthetic")
    store.grant("studio", SCOPE, Delegation("text consent", "test", continuous=True))
    store.configure_reference_policy("studio", SCOPE, enabled=True, consent_ref="media consent", expected_auth_version=1)
    runtime = HostRuntime(SimpleNamespace(config=SimpleNamespace(autonomy=None)))
    runtime.store = store
    runtime.media_policies["studio"] = (True, "media consent")

    async def on_chat(**kwargs):
        return await runtime.db("enqueue_wake", "studio", SCOPE, event_id="chat:" + kwargs["message_id"],
                                kind="chat", text=kwargs["text"], source_ref=kwargs["message_id"])

    runtime.artists["studio"] = SimpleNamespace(scope=SCOPE, on_chat=on_chat)
    runtime.signals["studio"] = asyncio.Event()
    yield runtime
    store.close()


async def chat(runtime, *, identity=None, message_id="trusted-event", text="", raw=None, **kwargs):
    return await runtime.on_chat(json.loads(SCOPE.snapshot()) if identity is None else identity, message_id,
                                 {"message_id": message_id, "processed_plain_text": text,
                                  "raw_message": [component()] if raw is None else raw, **kwargs})


def assets(runtime):
    return runtime.store.list_assets("studio", SCOPE)


async def test_image_only_import_is_idempotent_and_wakes_with_unreviewed_metadata(runtime):
    assert await chat(runtime)
    assert not await chat(runtime)
    assert len(assets(runtime)) == 1
    asset = runtime.store.get_asset("studio", SCOPE, assets(runtime)[0]["asset_id"])
    assert set(asset["roles"]) == {"character", "style", "composition", "palette"}
    assert asset["description"].endswith(host_media.UNREVIEWED_DESCRIPTION)
    wake = runtime.store.db.execute("SELECT * FROM artist_wakes").fetchone()
    assert wake["event_id"] == "chat:trusted-event" and wake["text"] == host_media.IMAGE_ONLY_TEXT
    assert runtime.signals["studio"].is_set() and not runtime.media_errors


@pytest.mark.parametrize("field,value", [("platform", "other"), ("stream_id", "other"), ("user_id", "other")])
async def test_wrong_exact_scope_is_inert(runtime, field, value):
    identity = json.loads(SCOPE.snapshot())
    identity[field] = value
    assert not await chat(runtime, identity=identity, text="normal text")
    assert not assets(runtime) and not runtime.signals["studio"].is_set()


@pytest.mark.parametrize("policy", [(False, ""), (False, "media consent"), (True, ""), (True, "  ")])
async def test_optin_and_separate_media_consent_required_but_text_continues(runtime, policy):
    runtime.media_policies["studio"] = policy
    assert not await chat(runtime)
    assert await chat(runtime, message_id="text-event", text="keep text")
    assert not assets(runtime)
    assert runtime.store.db.execute("SELECT text FROM artist_wakes").fetchone()[0] == "keep text"


@pytest.mark.parametrize("kwargs", [{"is_command": True}, {"is_notify": True}, {"text": " /command"}])
async def test_commands_and_notifications_do_not_import_or_wake(runtime, kwargs):
    assert not await chat(runtime, **kwargs)
    assert not assets(runtime)
    assert not runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()


async def test_malformed_media_has_bounded_code_and_does_not_break_text(runtime, caplog):
    assert await chat(runtime, text="normal text", raw=[component(b"sensitive malformed bytes")])
    assert runtime.media_errors == {"studio": "MEDIA_FORMAT_INVALID"}
    assert "sensitive" not in caplog.text and not assets(runtime)
    assert runtime.store.db.execute("SELECT text FROM artist_wakes").fetchone()[0] == "normal text"
    assert not await chat(runtime, message_id="bad-image-only", raw=[component(b"bad")])


async def test_invalid_batch_never_partially_imports(runtime):
    assert not await chat(runtime, raw=[component(), component(b"bad")])
    assert not assets(runtime)


async def test_duplicate_event_with_changed_bytes_does_not_overwrite_asset(runtime):
    assert await chat(runtime)
    assert not await chat(runtime, raw=[component(image_bytes("JPEG"))])
    assert len(assets(runtime)) == 1 and runtime.media_errors


async def test_import_preserves_direct_component_order_and_index(runtime):
    assert await chat(runtime, raw=[{"type": "text", "data": "context"}, component(),
                                   dict(component(), type="emoji"), component(image_bytes("JPEG"))])
    assert len(assets(runtime)) == 2
    sources = runtime.store.db.execute("SELECT event_id,message_index FROM external_assets ORDER BY message_index")
    assert [tuple(row) for row in sources] == [("chat:trusted-event", 1), ("chat:trusted-event", 3)]


async def test_revoke_and_regrant_during_decode_cannot_use_new_authority(runtime, monkeypatch):
    entered, release = Event(), Event()
    decode = host_media.current_images

    def paused(message):
        entered.set()
        assert release.wait(5)
        return decode(message)

    monkeypatch.setattr("image_plugin_under_test.host_runtime.current_images", paused)
    task = asyncio.create_task(chat(runtime))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        runtime.store.revoke("studio", SCOPE, source_ref="revoke", granted_by="test")
        runtime.store.grant("studio", SCOPE, Delegation("renewed text consent", "test", continuous=True))
        runtime.store.configure_reference_policy("studio", SCOPE, enabled=True, consent_ref="media consent",
                                                 expected_auth_version=3)
    finally:
        release.set()
    assert not await task
    assert runtime.media_errors == {"studio": "AUTH_CHANGED"}
    assert not assets(runtime) and not runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()


async def test_media_policy_revoked_during_decode_is_rechecked_by_store(runtime, monkeypatch):
    original = host_media.current_images

    def revoke_media(message):
        runtime.store.configure_reference_policy("studio", SCOPE, enabled=False, consent_ref="", expected_auth_version=1)
        return original(message)

    monkeypatch.setattr("image_plugin_under_test.host_runtime.current_images", revoke_media)
    assert not await chat(runtime)
    assert not assets(runtime) and runtime.media_errors


async def test_stopping_during_decode_prevents_new_import(runtime, monkeypatch):
    original = host_media.current_images

    def stop(message):
        runtime.stopping = True
        return original(message)

    monkeypatch.setattr("image_plugin_under_test.host_runtime.current_images", stop)
    assert not await chat(runtime)
    assert not assets(runtime)


async def test_arbitrary_store_error_is_bounded_without_leaking_data(runtime, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise StoreError("sensitive-unbounded-code" * 20, "sensitive raw bytes")

    monkeypatch.setattr(runtime.store, "import_asset", fail)
    assert await chat(runtime, text="text still works")
    assert runtime.media_errors == {"studio": "HOST_MEDIA_FAILED"}
    assert "sensitive" not in caplog.text and not assets(runtime)
