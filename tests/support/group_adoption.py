"""从群念头采用到工作室创作的脚本化测试场景。"""

from copy import deepcopy
import json

import pytest

from image_plugin_under_test.domain.policy import Scope
from .artist_fakes import BRIEF, Model
from .group_thought_host import (
    after,
    before,
    output,
    receipt,
)
from .host_chain import Harness, OWNER, PLATFORM, STREAM


HOST_SCOPE = Scope(PLATFORM, STREAM, OWNER)


class AdoptionModel(Model):
    """Script the choices, using only inspiration keys the Artist actually offers."""

    def __init__(self):
        super().__init__()
        self.wait = False
        self.force_create = False
        self.plans = []

    def plan(self, context):
        self.plans.append(deepcopy(context))
        notes = [idea for idea in context["inspirations"] if idea["origin"] == "group_thought"]
        active = next((work for work in context["works"]
                       if work["status"] not in {"finished", "abandoned"}), None)
        finished = [work for work in context["works"] if work["status"] == "finished"]
        if self.wait:
            decision = {"action": "wait", "reason": "先保留构思，暂时不画", "delay_seconds": 300}
        elif active and not self.force_create:
            decision = {"action": "continue", "work_id": active["work_id"], "reason": "继续原计划"}
        elif self.share and finished:
            decision = {"action": "share", "work_id": finished[-1]["work_id"], "reason": "愿意分享这幅画"}
        elif notes:
            chosen = notes[0]
            brief = {**BRIEF, "subject": chosen["summary"], "prompt": "用水彩描绘：" + chosen["summary"]}
            decision = {"action": "create", "reason": "这条构思适合画成水彩", "brief": brief,
                        "inspiration_keys": [chosen["key"]]}
        else:
            decision = {"action": "wait", "reason": "没有想采用的新构思"}
        return {"ideas": [], "decision": decision}

    async def __call__(self, request):
        if request["task"] == "plan":
            self.calls.append(deepcopy(request))
            return self.plan(request["context"])
        return await super().__call__(request)


@pytest.fixture
async def adoption_host(thought_host):
    h = thought_host
    h.adoption_model = AdoptionModel()

    async def capability(plugin_id, name, args):
        if name == "llm.generate":
            messages = args["prompt"]
            content = messages[1]["content"]
            if len(content) == 1 and "不得规划或覆盖" not in messages[0]["content"]:
                assert args["task_name"] == "utils"
                h.calls.append((name, deepcopy(args)))
                answer = h.adoption_model.plan(json.loads(content[0]["text"]))
                return {"success": True, "response": json.dumps(answer, ensure_ascii=False),
                        "model": "synthetic-group-adoption"}
        # Retain the existing synthetic multimodal review and raw Host receipt
        # handlers; they assert actual PNG bytes, routing and return_details.
        return await Harness.capability(h, plugin_id, name, args)

    for name in ("llm.generate", "send.image"):
        h.capabilities.register_capability(name, capability)
    await h.load(group=True)
    h.plugin.group_thought_runtime.clock = lambda: h.clock().timestamp()
    assert not h.gate.is_set()
    return h


async def remember(h, *, session=STREAM, tags=None):
    token, _ = await before(h, session)
    assert token
    items = output(h, token, tags)
    await after(h, items, session)
    result = await receipt(h, token, tags, session=session)
    assert result["success"] and result["structured_content"] == {"recorded": True}
    return items


async def sync(h, space="studio"):
    return await h.plugin.group_thought_runtime.sync(space)


async def advance(h, *, space="studio", seconds=300):
    h.clock.advance(seconds)
    await sync(h, space)
    return await h.runtime.artists[space].tick()
