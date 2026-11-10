"""可控时钟和合成的模型、绘图及发送替身。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

from image_plugin_under_test.creation.artist import RenderResult, render_digest
from image_plugin_under_test.domain.policy import Scope
from protocol_fixture import PNG


SCOPE = Scope("synthetic", "studio", "delegating-owner")
BRIEF = {
    "intent": "记录雨夜气氛",
    "subject": "窗边的小猫",
    "mood": "安静",
    "prompt": "画一只在雨夜窗边的小猫",
    "completion_criteria": "主体清楚且保留柔和灯光",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 2, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds=300):
        self.now += timedelta(seconds=seconds)


class Model:
    def __init__(self):
        self.calls = []
        self.share = False
        self.revise = 0
        self.review_errors = 0
        self.defer = False
        self.keep_first = False
        self.plan_action = None
        self.hook = None

    async def __call__(self, request):
        self.calls.append(request)
        if self.hook:
            await self.hook(request)
        context = request["context"]
        ideas = [
            {"summary": e["text"], "source_ids": [e["event_id"]]}
            for e in context.get("events", [])
            if e["kind"] == "chat"
        ][:8]
        if request["task"] == "inspire":
            return {"ideas": ideas}
        if request["task"] == "review":
            if context.get("review_contract") == 2:
                assert all(item["data"] == PNG and item["mime"] == "image/png" for item in request["images"])
            else:
                assert request["image"] == PNG
                assert request["mime"] == "image/png"
            if self.review_errors:
                self.review_errors -= 1
                raise RuntimeError("synthetic model failure")
            result = {
                "decision": "accept",
                "reason": "已达到完成标准",
                "strengths": ["主体清楚"],
                "issues": [],
            }
            if self.revise:
                self.revise -= 1
                result.update(
                    decision="revise",
                    issues=["背景过亮"],
                    edit_prompt="降低背景亮度",
                    preserve="保留小猫姿态",
                )
            if self.keep_first:
                result["best_version_id"] = context["versions"][0]["version_id"]
            if context.get("review_contract") == 2:
                result["comparison"] = {
                    "improvements": [], "regressions": list(result["issues"]), "preserved": ["小猫姿态"],
                    "evidence_version_ids": [item["version_id"] for item in request["images"]],
                }
            return result
        works = context["works"]
        active = next((w for w in works if w["status"] not in {"finished", "abandoned"}), None)
        if active:
            decision = {"action": "continue", "work_id": active["work_id"], "reason": "现在适合继续"}
            if self.plan_action == "wait":
                decision = {"action": "wait", "reason": "先休息", "delay_seconds": 600}
            elif self.plan_action == "abandon":
                decision["action"] = "abandon"
            if self.defer:
                self.defer = False
                decision.update(action="defer", delay_seconds=600)
        elif not works:
            decision = {
                "action": "create",
                "reason": "想画雨夜",
                "brief": dict(BRIEF),
                "inspiration_keys": [],
            }
            if not ideas:
                ideas = [{"summary": "自主练习雨夜暖光", "source_ids": []}]
        elif self.share and any(w["status"] == "finished" for w in works):
            decision = {"action": "share", "reason": "想分享完成的画", "work_id": works[-1]["work_id"]}
        else:
            decision = {"action": "wait", "reason": "先收藏，暂不分享"}
        return {"ideas": ideas, "decision": decision}


class Renderer:
    simulation = True
    supports_edit = True

    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.pending = False
        self.unknown_artifact = False
        self.lose_receipt = False
        self.lookup_missing = False
        self.corrupt = False
        self.before_return = None

    async def submit(self, *, scope, request_id, prompt, source):
        self.calls.append({"scope": scope, "request_id": request_id, "prompt": prompt, "source": source})
        params = {"prompt": prompt}
        if source:
            params.update(source_job_id=source["job_id"], source_sha256=source["sha256"])
        result = RenderResult(
            request_id,
            render_digest("edit" if source else "submit", params),
            "mock_job_" + str(len(self.calls)),
            "running" if self.pending else "completed",
            None if self.pending else PNG,
            sha256=hashlib.sha256(PNG).hexdigest(),
        )
        if self.unknown_artifact:
            result = replace(result, state="outcome_unknown", image=PNG)
        # 先保留服务端回执，再模拟客户端丢响应，供只读恢复查询原任务。
        self.jobs[request_id] = result
        if self.before_return:
            await self.before_return()
        if self.lose_receipt:
            raise TimeoutError("response lost after acceptance")
        return self._response(result)

    def _response(self, result):
        return replace(result, sha256="0" * 64) if self.corrupt else result

    async def status(self, *, scope, job_id):
        assert scope == SCOPE
        return self._response(next(r for r in self.jobs.values() if r.job_id == job_id))

    async def lookup(self, *, scope, request_id):
        assert scope == SCOPE
        return None if self.lookup_missing else self._response(self.jobs.get(request_id))

    def complete(self):
        self.jobs = {
            k: replace(r, state="completed", image=PNG, sha256=hashlib.sha256(PNG).hexdigest())
            for k, r in self.jobs.items()
        }


class Sender:
    simulation = True

    def __init__(self):
        self.calls = []
        self.lose_receipt = False

    async def send(self, *, scope, version):
        self.calls.append((scope, version))
        if self.lose_receipt:
            raise ConnectionError("send may already have happened")
        return True
