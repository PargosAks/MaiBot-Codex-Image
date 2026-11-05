"""Phase-one group collection. No production hook, timer, artist wake or model tool.

The simulation drives accept() from a trusted receipt source and tick() from a
clock. A future Host adapter must own this object's lifecycle, serialize ticks,
reload and close, and keep synchronous SQLite operations off the Host event loop.
No default reader is implemented on MaiBot 1.3.15.
"""

from pathlib import Path
import time

from .group_ingress import GroupBinding, GroupIngress, GroupIngressCandidate
from .group_notebook import GroupNotebook


class GroupCollection:
    def __init__(self, runtime, *, reader=None, clock=time.time):
        self._runtime = runtime
        self._config = runtime.plugin.config
        self._clock = clock
        self._since = clock()
        self._ingress = GroupIngress(reader, clock=clock)
        self._book = None
        self._active = set()
        self._closed = False

    def _routes(self):
        runtime = self._runtime
        cfg = runtime.plugin.config
        if (self._closed or runtime.stopping or runtime.plugin.artist_runtime is not runtime
                or cfg is not self._config or cfg.autonomy is not runtime.config
                or not cfg.plugin.enabled or not cfg.autonomy.enabled):
            return {}
        routes = {}
        # Do not use ordering to pick a recipient if two configured spaces
        # claim the same group or stream, even if only one opted into collection.
        groups, streams = set(), set()
        ambiguous_groups, ambiguous_streams = set(), set()
        for item in cfg.autonomy.spaces:
            if not item.group_id:
                continue
            group = (item.platform, item.group_id)
            stream = (item.platform, item.stream_id)
            if group in groups:
                ambiguous_groups.add(group)
            if stream in streams:
                ambiguous_streams.add(stream)
            groups.add(group)
            streams.add(stream)
        for item in cfg.autonomy.spaces:
            verified = runtime.verified_notebook_groups.get(item.space_id)
            if not verified or not item.group_inspiration or not item.continuous:
                continue
            group_id, scope, consent = verified
            if ((group_id, scope.platform, scope.stream_id, scope.user_id, consent) !=
                    (item.group_id, item.platform, item.stream_id, item.user_id, item.consent_ref)
                    or (scope.platform, group_id) in ambiguous_groups
                    or (scope.platform, scope.stream_id) in ambiguous_streams
                    or f"{scope.platform}:{scope.user_id}" not in cfg.connection.allowed_users):
                continue
            grant = runtime.store.get_space(item.space_id, scope)
            if grant["authorized"] and grant["auth_version"] == runtime.grant_versions[item.space_id]:
                routes[GroupBinding(scope.platform, scope.stream_id, group_id)] = (item.space_id, scope.user_id)
        return routes

    async def accept(self, candidate):
        if not isinstance(candidate, GroupIngressCandidate) or not isinstance(candidate.binding, GroupBinding):
            return False
        if not all(isinstance(v, str) for v in (candidate.binding.platform, candidate.binding.stream_id,
                                                 candidate.binding.group_id)):
            return False
        route = self._routes().get(candidate.binding)
        if route is None:
            return False
        confirmed = await self._ingress.confirm(candidate, binding=candidate.binding, enabled_since=self._since)
        if confirmed is None or self._routes().get(candidate.binding) != route:
            return False
        # No await between the last authority check and admission on this owner.
        space_id, owner_id = route
        if self._book is None:
            self._book = GroupNotebook(Path(self._runtime.plugin.ctx.paths.data_dir), clock=self._clock)
        binding = dict(space_id=space_id, platform=confirmed.binding.platform, stream_id=confirmed.binding.stream_id,
                       group_id=confirmed.binding.group_id, owner_id=owner_id)
        if space_id not in self._active:
            self._book.bind(**binding)
            self._active.add(space_id)
        return self._book.observe(**binding, member_id=confirmed.sender_id, message_id=confirmed.message_id,
                                  text=confirmed.text).accepted

    def tick(self):
        if self._book is None:
            return ()
        allowed = {space for space, _ in self._routes().values()}
        for space in self._active - allowed:
            self._book.clear_pending(space_id=space)
            self._active.remove(space)
        return self._book.flush_due()

    def close(self):
        self._closed = True
        if self._book is not None:
            self._book.close()
            self._book = None
        self._active.clear()
