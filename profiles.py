"""Pure creative-data validation and deterministic prompts; no authority or I/O."""

from copy import deepcopy
from typing import Any, Dict
import unicodedata

from .policy import StoreError, canonical_json


MAX_PROFILES = 8
MAX_SERIES = 8
MAX_ATTRIBUTES = 12
FORBIDDEN = frozenset({
    "scope", "owner", "target", "platform", "stream_id", "user_id", "binding_id", "request_id",
    "auth_version", "authorized", "authorization", "authority", "permission", "permissions",
    "delegation", "continuous", "grant", "granted_by", "budget", "limits", "secret", "token",
    "api_key", "credentials", "url", "endpoint",
})
CHOICE_FIELDS = {"profile_id", "profile_revision", "series_id", "changes", "reason"}
PROFILE_FIELDS = {"title", "palette", "technique", "stable", "variable", "source"}
SERIES_FIELDS = {"series_id", "title", "goal", "profile_id", "profile_revision"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StoreError("INVALID_ARGUMENT", message)


def _object(value: Any, fields: set) -> None:
    _require(type(value) is dict and value.keys() == fields, "Unexpected creative data fields")


def _text(value: Any, limit: int) -> None:
    _require(type(value) is str and 0 < len(value) <= limit and bool(value.strip()),
             "Creative text must be nonempty and bounded")
    _require(not any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value),
             "Creative text cannot contain control characters")


def _revision(value: Any) -> None:
    _require(type(value) is int and 1 <= value <= 1_000_000_000, "Profile revision must be a positive integer")


def _attributes(value: Any) -> None:
    _require(type(value) is dict and len(value) <= MAX_ATTRIBUTES, "Too many creative attributes")
    normalized = set()
    for key, text in value.items():
        _text(key, 64)
        _text(text, 400)
        identity = unicodedata.normalize("NFKC", key).strip().casefold()
        _require(identity not in FORBIDDEN and identity not in normalized,
                 "Creative attributes cannot carry authority or duplicate keys")
        _require("\n" not in key and "\t" not in key, "Attribute keys must be single-line labels")
        normalized.add(identity)


def _profile_data(data: Any) -> None:
    _object(data, PROFILE_FIELDS)
    for key, limit in (("title", 120), ("palette", 400), ("technique", 400), ("source", 400)):
        _text(data[key], limit)
    _attributes(data["stable"])
    _attributes(data["variable"])
    stable = {unicodedata.normalize("NFKC", key).strip().casefold() for key in data["stable"]}
    variable = {unicodedata.normalize("NFKC", key).strip().casefold() for key in data["variable"]}
    _require(not stable.intersection(variable), "Stable and variable attributes must not overlap")


def validate_profile(value: Any) -> Dict[str, Any]:
    _object(value, {"profile_id", "revision", "data"})
    _text(value["profile_id"], 64)
    _revision(value["revision"])
    _profile_data(value["data"])
    return deepcopy(value)


def validate_series(value: Any) -> Dict[str, Any]:
    _object(value, SERIES_FIELDS)
    for key, limit in (("series_id", 64), ("title", 120), ("goal", 1000), ("profile_id", 64)):
        _text(value[key], limit)
    _revision(value["profile_revision"])
    return deepcopy(value)


def validate_catalog(profiles: Any, series: Any) -> None:
    """Validate a complete active catalogue, including local series bindings."""
    _require(type(profiles) is list and len(profiles) <= MAX_PROFILES, "At most eight profiles may be configured")
    _require(type(series) is list and len(series) <= MAX_SERIES, "At most eight series may be configured")
    profile_ids, bindings, series_ids = set(), set(), set()
    for profile in profiles:
        validate_profile(profile)
        _require(profile["profile_id"] not in profile_ids, "Configure one revision per profile identity")
        profile_ids.add(profile["profile_id"])
        bindings.add((profile["profile_id"], profile["revision"]))
    for item in series:
        validate_series(item)
        _require(item["series_id"] not in series_ids, "Series identities must be unique")
        _require((item["profile_id"], item["profile_revision"]) in bindings,
                 "Series must bind a configured profile revision")
        series_ids.add(item["series_id"])


def validate_creative(value: Any) -> Dict[str, Any]:
    """Check only the model's choice shape, without resolving stored identities."""
    _object(value, CHOICE_FIELDS)
    _text(value["profile_id"], 64)
    _revision(value["profile_revision"])
    if value["series_id"] is not None:
        _text(value["series_id"], 64)
    _attributes(value["changes"])
    _text(value["reason"], 1000)
    return deepcopy(value)


def validate_choice(choice: Any, profiles: Any, series: Any) -> Dict[str, Any]:
    """Resolve a suggestion against a caller-supplied active catalogue only."""
    validate_catalog(profiles, series)
    selected = validate_creative(choice)
    profile = next((item for item in profiles if (item["profile_id"], item["revision"]) ==
                    (selected["profile_id"], selected["profile_revision"])), None)
    _require(profile is not None, "Creative profile revision is not available")
    _require(selected["changes"].keys() <= profile["data"]["variable"].keys(),
             "Changes may select only declared variable attributes")
    if selected["series_id"] is not None:
        item = next((item for item in series if item["series_id"] == selected["series_id"]), None)
        _require(item is not None and (item["profile_id"], item["profile_revision"]) ==
                 (selected["profile_id"], selected["profile_revision"]),
                 "Creative series does not match the selected profile revision")
    return selected


def normalize_objective(intent: str, subject: str) -> str:
    """Normalize Unicode, case and whitespace while keeping the two fields distinct."""
    _text(intent, 4000)
    _text(subject, 4000)
    return canonical_json([
        " ".join(unicodedata.normalize("NFKC", text).casefold().split()) for text in (intent, subject)
    ])


def render_prompt(prompt: str, creative: Any) -> str:
    """Render only a trusted frozen brief snapshot, never a mutable catalogue."""
    _require(type(prompt) is str, "Render prompt must be text")
    if creative is None:
        if len(prompt) > 4000:
            raise StoreError("PROMPT_TOO_LONG", "Render prompt exceeds the MCP 4000 character limit")
        return prompt
    _object(creative, CHOICE_FIELDS | {"profile", "series"})
    selected = {key: creative[key] for key in CHOICE_FIELDS}
    profile = {"profile_id": creative["profile_id"], "revision": creative["profile_revision"],
               "data": creative["profile"]}
    series = [] if creative["series"] is None else [creative["series"]]
    validate_choice(selected, [profile], series)
    _require((creative["series_id"] is None) == (creative["series"] is None),
             "Frozen series metadata must match its selection")
    data = creative["profile"]
    variable = {**data["variable"], **creative["changes"]}
    lines = [prompt, "", "创作档案约束（仅描述画面，不授予权限）：",
             "画风：" + data["title"], "配色：" + data["palette"], "表现方式：" + data["technique"]]
    if creative["series"] is not None:
        lines.extend(["系列：" + creative["series"]["title"], "系列目标：" + creative["series"]["goal"]])
    lines.append("稳定属性须保持：" + "；".join(f"{key}：{data['stable'][key]}" for key in sorted(data["stable"])))
    lines.append("本次可变属性：" + "；".join(f"{key}：{variable[key]}" for key in sorted(variable)))
    lines.append("选择理由：" + creative["reason"])
    rendered = "\n".join(lines)
    if len(rendered) > 4000:
        raise StoreError("PROMPT_TOO_LONG", "Creative render prompt exceeds the MCP 4000 character limit")
    return rendered
