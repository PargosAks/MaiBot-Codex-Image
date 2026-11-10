"""供模型协议与工作区测试共用的请求上下文和回执。"""



BRIEF = {"intent": "练习光影", "subject": "窗边的花", "mood": "宁静", "prompt": "画窗边的花", "completion_criteria": "主体清晰"}


CONTEXT = {
    "events": [{"event_id": "chat1", "kind": "chat", "text": "喜欢花", "source_ref": "message1"},
               {"event_id": "idle1", "kind": "idle", "text": "", "source_ref": None}],
    "inspirations": [{"key": "flower", "summary": "花", "origin": "chat", "source_refs": ["chat1"]}],
    "works": [{"work_id": "work1", "status": "planned", "brief": BRIEF, "reason": "练习", "best_version": None}],
    "versions": [{"version_id": "v1", "parent_id": None, "sha256": "a" * 64, "mime": "image/png"}],
    "persona": "安静的小画师",
}


ACCEPT = {"decision": "accept", "reason": "达到目标", "strengths": ["主体明确"], "issues": [], "best_version_id": "v1"}
