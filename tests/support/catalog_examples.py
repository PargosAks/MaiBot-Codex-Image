"""共用的创意目录配置和模型决策样本。"""



PROFILES = [{"profile_id": "rain", "revision": 1, "data": {
    "title": "Rain studies", "palette": "warm amber", "technique": "watercolor",
    "stable": {"subject": "cat by window"}, "variable": {"light": "soft"}, "source": "configured",
}}]


SERIES = [{"series_id": "windows", "title": "Window series", "goal": "Explore changing weather",
           "profile_id": "rain", "profile_revision": 1}]


CHOICE = {"profile_id": "rain", "profile_revision": 1, "series_id": "windows",
          "changes": {"light": "moonlight"}, "reason": "Continue the weather study"}


DATA = PROFILES[0]["data"]


NEW = {"action": "create_profile", "reason": "Explore a personal style", "data": DATA}
