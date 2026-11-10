"""固定公开 Host 的来源验证和隔离 AST 装载。"""

from pathlib import Path
import ast
import hashlib
import os
import subprocess

import pytest


HOST_REV = "062d45cf8bd4272ac57d7ec03fcf405e5a458211"
HOST_REPO_ENV = "CODEX_IMAGE_TEST_HOST_REPO"
HOST_SOURCE_ENV = "CODEX_IMAGE_TEST_HOST_SOURCE"

# 由 HOST_REV 的公开 Git blob 计算：忽略 UTF-8 BOM 和 CRLF，仅核对实际执行的文件。
# 添加从目录快照加载的公开方法时，必须从同一提交补充对应文件的指纹。
HOST_SHA256 = {
    "chat/heart_flow/heartflow_message_processor.py":
        "2fcf22648a5321dd79fddf94f37fa3216dca7e2266f1265424b8b94b6998f708",
    "chat/message_receive/bot.py":
        "39a412664eb0dbf8891185b333695298c9115c0cfb4ff8d3cead54be9374b87c",
    "common/data_models/message_component_data_model.py":
        "4b52f20c53a3b84e3af9a15108bc153c3c0151d3581f0eda1e1400b956fbde4b",
    "common/utils/utils_message.py":
        "9db8ff2b7f133f5cb4aaa94c5c450055aa060020fa01b4bac33129d8616b21c3",
    "llm_models/payload_content/context_item.py":
        "dce39edebdd04d536a439e8549dd2eed824a3b8dd7cc0b0fd760081feffc4814",
    "llm_models/payload_content/context_protocol.py":
        "7eeca0220913f31c6ba6dc1e6fdca770fc4b3928f27e756d26174ff3f1c53802",
    "llm_models/payload_content/context_snapshot.py":
        "b871476085d36084bb6478d8b8eb379394da82753b5f683a4f2ac737563b0b17",
    "llm_models/payload_content/tool_option.py":
        "133b69008a601066be3d1204687260ac96369435d2d058b221fef778959d3f2d",
    "maisaka/chat_loop_service.py":
        "6e68031e9963b7f5c3effb06957637508f7116ce413637ba8a39aee93de9d9b0",
    "maisaka/reasoning_engine.py":
        "b5973fe25d869609f15470ce997152cedb2a75f0ae755909b6e081b1bad1b150",
    "plugin_runtime/component_query.py":
        "8ab651ca8e11d19cf3443a752390715e1f73793fd6a08891538ab612d41662e0",
    "plugin_runtime/hook_payloads.py":
        "c29fbb1ea72cb4f0542913e9a15dcf3b0454802da9170fea71fb9241553cfd81",
    "plugin_runtime/hook_schema_utils.py":
        "0fff33de7590dd97b49b6eb6e14d6a30cdde72d1b3770f48461adacbe897d4d8",
    "plugin_runtime/host/authorization.py":
        "f8632ec7aae8a9cb12effa2573407280acf3f558c491ed3cf3c075df2c5d8977",
    "plugin_runtime/host/capability_service.py":
        "d26caadd1d2cdfc608dfe9a6f0e23792d26220a2bf0b6436437f144430c268df",
    "plugin_runtime/host/hook_dispatcher.py":
        "f84989f1c6853c718221c4700f927b12bb48436b8d1e6dc436a0c63ed272d6a6",
    "plugin_runtime/host/hook_spec_registry.py":
        "4471369de6a4773d304ce8d23e09d48bdbda700716968910a5bdf7f869efb0f8",
    "plugin_runtime/host/message_utils.py":
        "753a357d0a0d412a0a0b55098e882a00d8d8c1cea2a980ca3f782ac61b1bde7e",
    "plugin_runtime/protocol/envelope.py":
        "e9a2a73fa05dd604abed4b7b7f6b85f907fca60db07b22e9a8a07bb63928933e",
    "plugin_runtime/protocol/errors.py":
        "83290bf17e07e2f0f1206fa5abf430846c096e402b16525de2fff3f9c26d10b7",
    "plugin_runtime/runner/runner_main.py":
        "7d0dde0b0edeb68e5cabc9de0f889cb732f9ea65c76720b17af39eeb2bc54894",
}


def fixed_host_file(relative: str) -> str:
    """从固定 Git 对象读取；Studio 验收不接受目录覆盖。"""
    repository = os.environ.get(HOST_REPO_ENV)
    if repository is None:
        pytest.skip(f"Set {HOST_REPO_ENV} to a public MaiBot Git repository containing {HOST_REV}")
    assert repository.strip(), f"Explicit {HOST_REPO_ENV} must name a public MaiBot Git repository"
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", repository, "show", f"{HOST_REV}:src/{relative}"],
        capture_output=True, check=False,
    )
    assert result.returncode == 0, f"Required public Host Git object {HOST_REV}:src/{relative} is unavailable"
    return result.stdout.decode("utf-8-sig")


def host_file(relative: str) -> str:
    explicit = os.environ.get(HOST_SOURCE_ENV)
    if not explicit:
        return fixed_host_file(relative)
    expected = HOST_SHA256.get(relative)
    assert expected, f"No {HOST_REV} fingerprint registered for public Host file {relative}"
    text = (Path(explicit) / relative).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # 明确提供的源码不匹配时失败；禁止回退到 Git，以免掩盖选错快照。
    assert actual == expected, (
        f"{HOST_SOURCE_ENV}/{relative} does not match Host {HOST_REV}: "
        f"expected SHA-256 {expected}, got {actual}"
    )
    return text


def load_nodes(namespace, text, names, methods=None):
    nodes = []
    for node in ast.parse(text).body:
        name = getattr(node, "name", None)
        if name in names:
            if methods and name in methods:
                node.body = [item for item in node.body if getattr(item, "name", None) in methods[name]]
            nodes.append(node)
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<fixed-public-host>", "exec"), namespace)
