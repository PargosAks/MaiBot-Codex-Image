"""Read-only autonomous thoughts: temporary SQLite state and a Node DOM only."""

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.domain import store as artist_store
from image_plugin_under_test.inspiration.group_notebook import GroupNotebook
from image_plugin_under_test.inspiration.group_thoughts import GroupThoughts
from image_plugin_under_test.domain.policy import Delegation, Scope, StoreError
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.workspace.studio import Studio

ROOT = Path(__file__).resolve().parents[1]
OWNER = Scope("synthetic", "thought-stream-a", "delegator")
OTHER = Scope("synthetic", "thought-stream-b", "other-delegator")
START = datetime(2026, 11, 3, 1, tzinfo=UTC).timestamp()


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def reader(root, *, scope=OWNER, group_id="group-a"):
    return Studio(root, scopes={"a": scope}, group_bindings={"a": group_id}, enabled=False)


@pytest.fixture
def thought_data(tmp_path):
    store = ArtistStore(tmp_path / "artist.sqlite3")
    for key, scope in (("a", OWNER), ("b", OTHER)):
        store.create_space(key, scope, binding_id="synthetic-service")
    store.close()
    now = [START]
    thoughts = GroupThoughts(tmp_path, clock=lambda: now[0])
    try:
        for index, tags in enumerate((["rain"], ["forest"], ["store"], ["rain"])):
            result = thoughts.record(space_id="a", platform=OWNER.platform, group_id="group-a",
                                     stream_id=OWNER.stream_id, owner_id=OWNER.user_id, tags=tags,
                                     request_id=hashlib.sha256(f"synthetic-a-{index}".encode()).hexdigest())
            assert result["accepted"], result
            now[0] += 601
        result = thoughts.record(space_id="b", platform=OTHER.platform, group_id="group-b",
                                 stream_id=OTHER.stream_id, owner_id=OTHER.user_id, tags=["night"],
                                 request_id=hashlib.sha256(b"synthetic-b").hexdigest())
        assert result["accepted"], result
    finally:
        thoughts.close()
    # Persist an old experimental topic alongside the independent thought DB.
    notebook = GroupNotebook(tmp_path, clock=lambda: now[0])
    try:
        notebook.bind(space_id="a", platform=OWNER.platform, group_id="group-a",
                      stream_id=OWNER.stream_id, owner_id=OWNER.user_id)
        for index in range(3):
            assert notebook.observe(space_id="a", platform=OWNER.platform, group_id="group-a",
                                    stream_id=OWNER.stream_id, owner_id=OWNER.user_id,
                                    member_id=f"private-member-{index}", message_id=f"private-message-{index}",
                                    text="下雨 private-original").accepted
        now[0] += 601
        notebook.flush()
    finally:
        notebook.close()
    return tmp_path, tmp_path / "group_thoughts.sqlite3"


def test_thoughts_are_scoped_bounded_paginated_and_do_not_change_any_state(thought_data, monkeypatch):
    root, _ = thought_data
    before = snapshot(root)
    for writer in (ArtistStore, GroupThoughts, GroupNotebook):
        monkeypatch.setattr(writer, "__init__", lambda *a, **kw: pytest.fail("writer constructed by UI"))
    view = reader(root)
    first = view.records("a", "group_thoughts", limit=2)
    assert first["available"] and first["has_more"] and first["next_offset"] == 2
    assert [item["summary"] for item in first["items"]] == ["雨景", "便利店"]
    rain = first["items"][0]
    assert set(rain) == {"note_id", "summary", "source", "created_at", "updated_at", "adoption_status", "work_id"}
    assert rain["source"] == "MaiBot 群聊构思" and rain["adoption_status"] == "unadopted"
    assert rain["work_id"] is None
    assert rain["created_at"] == "2026-11-03T01:00:00Z"
    assert rain["updated_at"] == datetime.fromtimestamp(START + 1803, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    second = view.records("a", "group_thoughts", offset=first["next_offset"], limit=2)
    assert len(second["items"]) == 1 and "山林" in second["items"][0]["summary"]
    assert not second["has_more"] and second["next_offset"] is None
    assert view.records("a", "group_thoughts", offset=10000)["items"] == []
    assert view.records("a", "group_thoughts", limit=2) == first
    experiment = view.records("a", "group_inspirations")
    assert experiment["items"][0]["inspiration_id"] == "rain"
    assert experiment["items"][0]["message_count"] == 3
    assert snapshot(root) == before
    payload = json.dumps(first) + json.dumps(second)
    assert all(secret not in payload for secret in ("private-", OTHER.user_id, OWNER.user_id,
                                                   OWNER.stream_id, "member", "count", "request"))


def import_thought(root, *, space_id="a", scope=OWNER, group_id="group-a", note=None, adopt=False):
    """Use the real atomic source import/plan path, with no render or sender."""
    note = note or reader(root).records("a", "group_thoughts")["items"][0]
    store = ArtistStore(root / "artist.sqlite3")
    try:
        version = store.grant(space_id, scope, Delegation("synthetic-consent", scope.user_id, True))
        store.sync_group_thoughts(space_id, scope, group_id=group_id, enabled=True,
                                  thoughts=[{"note_id": note["note_id"], "summary": note["summary"]}],
                                  expected_auth_version=version)
        work_id = "planned-" + space_id
        if adopt:
            store.plan_work(space_id, scope, work_id=work_id,
                            brief={"subject": "雨景", "intent": "记录群聊构思", "mood": "安静",
                                   "prompt": "画雨中的窗", "completion_criteria": "可辨认雨景"},
                            inspiration_keys=["group-thought:" + note["note_id"]],
                            expected_auth_version=version)
        return note, work_id
    finally:
        store.close()


def test_imported_but_unselected_thought_remains_unadopted_without_writes(thought_data):
    root, _ = thought_data
    note, _ = import_thought(root)
    before = snapshot(root)
    item = next(item for item in reader(root).records("a", "group_thoughts")["items"]
                if item["note_id"] == note["note_id"])
    assert item["adoption_status"] == "unadopted" and item["work_id"] is None
    assert snapshot(root) == before


def test_plan_adoption_is_visible_before_any_render_even_when_disabled_and_revoked(thought_data):
    root, _ = thought_data
    note, work_id = import_thought(root, adopt=True)
    store = ArtistStore(root / "artist.sqlite3")
    try:
        assert store.get_artist_work("a", OWNER, work_id)["status"] == "planned"
        assert store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        store.revoke("a", OWNER, source_ref="synthetic-revoke", granted_by=OWNER.user_id)
    finally:
        store.close()
    before = snapshot(root)
    page = reader(root).records("a", "group_thoughts")
    item = next(item for item in page["items"] if item["note_id"] == note["note_id"])
    assert item["adoption_status"] == "adopted" and item["work_id"] == work_id
    assert all(other["adoption_status"] == "unadopted" and other["work_id"] is None
               for other in page["items"] if other["note_id"] != note["note_id"])
    assert reader(root).records("a", "group_thoughts") == page
    assert snapshot(root) == before


@pytest.mark.parametrize("version", range(1, 8))
def test_actual_legacy_schema_reads_notes_without_links_migrations_or_writes(thought_data, version):
    root, _ = thought_data
    path = root / "artist.sqlite3"
    path.unlink()  # Replace only the temporary fixture ledger with a real old schema.
    with closing(sqlite3.connect(path)) as db, db:
        for statement in artist_store._SCHEMA:
            db.execute(statement)
        for migration in range(2, version + 1):
            for statement in getattr(artist_store, f"_SCHEMA_{migration}"):
                db.execute(statement)
        db.execute("INSERT INTO spaces(space_id,scope,binding_id) VALUES(?,?,?)",
                   ("a", OWNER.snapshot(), "synthetic-service"))
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(root)
    page = reader(root).records("a", "group_thoughts")
    assert page["available"] and len(page["items"]) == 3
    assert all(item["adoption_status"] == "unadopted" and item["work_id"] is None for item in page["items"])
    assert snapshot(root) == before


def test_same_note_in_another_group_never_exposes_its_work(thought_data):
    root, _ = thought_data
    note = reader(root).records("a", "group_thoughts")["items"][0]
    other = Studio(root, scopes={"b": OTHER}, group_bindings={"b": "group-b"})
    other_note = other.records("b", "group_thoughts")["items"][0]
    _, hidden_work = import_thought(root, space_id="b", scope=OTHER, group_id="group-b", note=other_note, adopt=True)
    # A forged persisted association cannot widen read authority, even if it
    # names a source visible in this reader's group. Real imports reject this.
    with closing(sqlite3.connect(root / "artist.sqlite3")) as db, db:
        db.execute("UPDATE group_thought_links SET note_id=? WHERE space_id='b'", (note["note_id"],))
    before = snapshot(root)
    page = reader(root).records("a", "group_thoughts")
    assert all(item["adoption_status"] == "unadopted" and item["work_id"] is None for item in page["items"])
    assert hidden_work not in json.dumps(page) and snapshot(root) == before


@pytest.mark.parametrize("table,field,value", [
    ("group_thought_policy", "scope_snapshot", OTHER.snapshot()),
    ("group_thought_policy", "platform", "other-platform"),
    ("group_thought_policy", "stream_id", "other-stream"),
    ("group_thought_policy", "group_id", "other-group"),
    ("group_thought_links", "group_id", "other-group"),
    ("group_thought_links", "adopted_by", "hidden-orphan-work"),
])
def test_adoption_requires_complete_matching_source_policy_and_work(thought_data, table, field, value):
    root, _ = thought_data
    import_thought(root, adopt=True)
    with closing(sqlite3.connect(root / "artist.sqlite3")) as db, db:
        db.execute(f"UPDATE {table} SET {field}=? WHERE space_id='a'", (value,))
    before = snapshot(root)
    page = reader(root).records("a", "group_thoughts")
    assert all(item["adoption_status"] == "unadopted" and item["work_id"] is None for item in page["items"])
    assert "hidden-orphan-work" not in json.dumps(page) and snapshot(root) == before


@pytest.mark.parametrize("field,value", [("user_id", "other-owner"), ("platform", "other-platform"),
                                        ("stream_id", "other-stream")])
def test_thoughts_require_exact_persisted_artist_owner(thought_data, field, value):
    root, _ = thought_data
    fields = {"platform": OWNER.platform, "stream_id": OWNER.stream_id, "user_id": OWNER.user_id, field: value}
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root, scope=Scope(**fields)).records("a", "group_thoughts")
    assert snapshot(root) == before


@pytest.mark.parametrize("field,value", [("owner_id", "other-owner"), ("platform", "other-platform"),
                                        ("stream_id", "other-stream"), ("group_id", "other-group")])
def test_thought_binding_must_match_ledger_and_current_config(thought_data, field, value):
    root, _ = thought_data
    scope = Scope(value if field == "platform" else OWNER.platform,
                  value if field == "stream_id" else OWNER.stream_id,
                  value if field == "owner_id" else OWNER.user_id)
    with closing(sqlite3.connect(root / "artist.sqlite3")) as db, db:
        db.execute("UPDATE spaces SET scope=? WHERE space_id='a'", (scope.snapshot(),))
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root, scope=scope, group_id=value if field == "group_id" else "group-a").records("a", "group_thoughts")
    assert snapshot(root) == before


def test_chat_scopes_and_orphaned_topics_do_not_grant_thought_read_authority(thought_data):
    root, path = thought_data
    before = snapshot(root)
    assert not Studio(root, scopes={"a": OWNER}).records("a", "group_thoughts")["available"]
    assert snapshot(root) == before
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM thought_groups WHERE space_id='a'")
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root).records("a", "group_thoughts")
    assert snapshot(root) == before


@pytest.mark.parametrize("missing", ["artist.sqlite3", "group_thoughts.sqlite3"])
def test_missing_database_is_unavailable_without_creating_it(thought_data, missing):
    root, _ = thought_data
    (root / missing).unlink()  # Only synthetic fixture state is removed.
    before = snapshot(root)
    assert not reader(root).records("a", "group_thoughts")["available"]
    assert snapshot(root) == before


def test_missing_directory_is_not_created(tmp_path):
    root = tmp_path / "absent"
    assert not reader(root).records("a", "group_thoughts")["available"]
    assert not root.exists()


@pytest.mark.parametrize("version", [0, 2, 999])
def test_unknown_thought_schema_is_rejected_without_migration(thought_data, version):
    root, path = thought_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(root)
    with error("SCHEMA_UNSUPPORTED"):
        reader(root).records("a", "group_thoughts")
    assert snapshot(root) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_linked_thought_database_or_sidecar_is_rejected(thought_data, monkeypatch, suffix):
    root, _ = thought_data
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink",
                        lambda path: path.name == "group_thoughts.sqlite3" + suffix or original(path))
    before = snapshot(root)
    with error("UNSAFE_STATE_PATH"):
        reader(root).records("a", "group_thoughts")
    assert snapshot(root) == before


def test_malformed_thought_schema_is_read_failure_not_an_empty_success(thought_data):
    root, path = thought_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE thought_topics")
    before = snapshot(root)
    with error("STATE_READ_FAILED"):
        reader(root).records("a", "group_thoughts")
    assert snapshot(root) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_regular_unexpected_wal_sidecars_are_preserved_and_not_attached(thought_data, monkeypatch, suffix):
    root, path = thought_data
    Path(str(path) + suffix).write_bytes(b"synthetic-unknown-sidecar")
    before = snapshot(root)
    connect = sqlite3.connect
    statements = []

    def trace_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", trace_connect)
    with error("UNSAFE_STATE_PATH"):
        reader(root).records("a", "group_thoughts")
    assert not any(sql.startswith("ATTACH") for sql in statements)
    assert snapshot(root) == before


def test_wal_database_without_sidecars_cannot_create_them_when_read(thought_data):
    root, path = thought_data
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    before = snapshot(root)
    with error("SCHEMA_UNSUPPORTED"):
        reader(root).records("a", "group_thoughts")
    assert snapshot(root) == before


def test_thoughts_query_only_never_reads_requests_counts_or_experiment(thought_data, monkeypatch):
    root, _ = thought_data
    connect = sqlite3.connect
    statements, reads = [], []

    def readonly_connect(path, *args, **kwargs):
        assert "mode=ro" in str(path) and kwargs["uri"] is True
        db = connect(path, *args, **kwargs)
        db.set_trace_callback(statements.append)

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ:
                reads.append((database, table, column))
                assert table != "thought_requests" and not table.startswith("notebook_")
                assert column not in {"record_count", "request_id", "fingerprint"}
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    before = snapshot(root)
    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    assert reader(root).records("a", "group_thoughts")["items"]
    assert any(database == "thoughts" for database, _, _ in reads)
    assert "PRAGMA query_only=ON" in statements
    assert all("mode=ro" in sql for sql in statements if sql.startswith("ATTACH"))
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)
    assert snapshot(root) == before


def test_thought_text_is_bounded_at_sql_projection(thought_data):
    root, path = thought_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE thought_topics SET title=? WHERE space_id='a'", ("鲸" * 20000,))
    before = snapshot(root)
    page = reader(root).records("a", "group_thoughts")
    assert all(len(item["summary"]) == 1000 and item["adoption_status"] == "unadopted" for item in page["items"])
    assert snapshot(root) == before


@pytest.mark.parametrize("offset,limit", [(-1, 1), (10001, 1), (True, 1), (0, 0), (0, 51), (0, True)])
def test_invalid_thought_pagination_rejected_before_io(tmp_path, monkeypatch, offset, limit):
    view = reader(tmp_path)
    monkeypatch.setattr(view, "_open", lambda: pytest.fail("invalid query accessed storage"))
    with error("INVALID_ARGUMENT"):
        view.records("a", "group_thoughts", offset=offset, limit=limit)


def test_foreign_space_and_work_filter_rejected_before_io(tmp_path, monkeypatch):
    view = reader(tmp_path)
    monkeypatch.setattr(view, "_open", lambda: pytest.fail("invalid query accessed storage"))
    with error("NOT_FOUND"):
        view.records("b", "group_thoughts")
    with error("INVALID_ARGUMENT"):
        view.records("a", "group_thoughts", work_id="work")


async def test_private_wrapper_uses_real_configured_group_without_starting_runtime(thought_data, monkeypatch):
    root, _ = thought_data
    plugin = CodexImagePlugin()
    config = plugin.build_default_config()
    config["autonomy"]["spaces"] = [{"space_id": "a", "platform": OWNER.platform,
                                     "stream_id": OWNER.stream_id, "user_id": OWNER.user_id,
                                     "group_id": "group-a"}]
    plugin.set_plugin_config(config)
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=root)))
    for writer in (ArtistStore, GroupThoughts, GroupNotebook):
        monkeypatch.setattr(writer, "__init__", lambda *a, **kw: pytest.fail("writer constructed by UI"))
    before = snapshot(root)
    assert (await plugin.studio_records("a", "group_thoughts"))["items"]
    config["autonomy"]["spaces"][0]["group_id"] = ""
    plugin.set_plugin_config(config)
    assert not (await plugin.studio_records("a", "group_thoughts"))["available"]
    config["autonomy"]["spaces"][0]["group_id"] = "other-group"
    plugin.set_plugin_config(config)
    with error("NOT_FOUND"):
        await plugin.studio_records("a", "group_thoughts")
    assert plugin.artist_runtime is None and plugin.bridge is None and plugin.journal is None
    assert snapshot(root) == before


def test_chat_summary_does_not_read_or_include_thoughts(thought_data, monkeypatch):
    root, _ = thought_data
    def clock():
        return datetime.fromtimestamp(START, UTC)
    chat_view = Studio(root, scopes={"a": OWNER}, clock=clock, enabled=False)
    admin_view = Studio(root, scopes={"a": OWNER}, group_bindings={"a": "group-a"}, clock=clock, enabled=False)
    connect = sqlite3.connect
    statements = []

    def trace_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    before = snapshot(root)
    monkeypatch.setattr(sqlite3, "connect", trace_connect)
    chat_summary = chat_view.summary("a")
    assert "sharing" not in chat_summary
    assert not any("group_share_policy" in sql.lower() for sql in statements)
    admin_summary = admin_view.summary("a")
    assert admin_summary.pop("sharing") == {"mode": "collect_only", "allowed": False}
    assert chat_summary == admin_summary
    assert not any("thought" in sql.lower() or sql.startswith("ATTACH") for sql in statements)
    assert snapshot(root) == before


def test_webui_thoughts_use_existing_private_query_without_actions():
    manifest = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
    assert len(manifest["pages"]) == 1
    page = manifest["pages"][0]
    assert page["placement"] == "workspace" and page["entry"] == "assets/studio.html"
    assert set(page["queries"]) == {"spaces", "summary", "records", "image"}
    records = page["queries"]["records"]
    assert records["api"] == "studio_records" and records["version"] == "1"
    assert {"group_thoughts", "group_inspirations"} <= set(records["parameters"]["section"]["choices"])
    assert not page["actions"] and not page["uploads"]
    assert "实验" in page["description"] and "不作为生产收信" in page["description"]


def test_thought_page_safe_text_pagination_and_experiment_separation_without_browser():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for the isolated Studio DOM harness")
    html = (ROOT / "assets/studio.html").read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]+)</script>", html).group(1)
    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const p=JSON.parse(fs.readFileSync(0,'utf8')),elements=new Map(),calls=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.value='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[];this.append(...items);}
 setAttribute(k,v){this[k]=v;}
 set innerHTML(value){throw new Error('HTML injection sink');}
 addEventListener(){}
 close(){}
}
const document={createElement:tag=>new Element(tag),getElementById:id=>{
 if(!elements.has(id))elements.set(id,new Element(id==='spaces'?'select':'div'));
 return elements.get(id);
}};
const summary={authorization:{authorized:false,enabled:false},waiting:[],budget:{},counts:{},
 recent_works:[],current_work:null,day:'synthetic-day',coverage:'synthetic-coverage'};
const item={summary:'<img src=x onerror="throw 1">synthetic-thought',adoption_status:'unadopted',
 work_id:'private-unadopted-work',
 created_at:'2026-11-03T01:00:00Z',updated_at:'2026-11-03T02:00:00Z',
 participant_count:998,message_count:997,members:['private-member'],raw_text:'private-original',
 source_ref:'private-request-id',source:'untrusted-source'};
const maibot={ready:Promise.resolve(),async query(name,args){
 calls.push({name,args});
 if(name==='spaces')return {items:[{space_id:'a'},{space_id:'b'}]};
 if(name==='summary')return summary;
 assert.equal(name,'records');
 if(args.section!=='group_thoughts')return {items:[],available:false,has_more:false,next_offset:null};
 return {items:[{...item,summary:item.summary+':'+args.space_id+':'+args.offset,
 adoption_status:args.space_id==='b'?'adopted':'unadopted',
 work_id:args.space_id==='b'?'<svg onload="throw 1">planned-b':item.work_id}],available:true,
 has_more:args.offset===0,next_offset:args.offset===0?12:null};
}};
vm.runInNewContext(p.script,{document,maibot,URL:{revokeObjectURL(){}}});
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(setImmediate);};
const text=el=>el.textContent+' '+el.children.map(text).join(' ');
(async()=>{
 await settle();
 const tabs=elements.get('tabs').children;
 const tab=tabs.find(x=>x.dataset.section==='group_thoughts');
 assert.equal(tab.textContent,'创作念头');tab.onclick();await settle();
 assert.equal(elements.get('error').textContent,'');
 assert(text(elements.get('filter')).includes('自主创作念头'));
 assert(text(elements.get('filter')).includes('不代表生成或分享成功'));
 const body=text(elements.get('records'));
 for(const label of ['来源','MaiBot 群聊构思','主题','记录时间','更新时间','未采用',item.summary,
                     item.created_at,item.updated_at])assert(body.includes(label),label);
 for(const hidden of ['参与人数','成员数','消息数','998','997','private-member','private-original',
                      'private-request-id','untrusted-source','private-unadopted-work'])assert(!body.includes(hidden),hidden);
 assert(!elements.get('records').children.some(x=>x.tag==='img'));
 assert(!elements.get('next').disabled);
 elements.get('next').onclick();await settle();
 assert(text(elements.get('records')).includes(':a:12'));assert(elements.get('next').disabled);
 elements.get('prev').onclick();await settle();assert(text(elements.get('records')).includes(':a:0'));
 elements.get('spaces').value='b';elements.get('spaces').onchange();await settle();
 assert(text(elements.get('records')).includes(':b:0'));assert(!text(elements.get('records')).includes(':a:'));
 assert(text(elements.get('records')).includes('已采用（已立项）'));
 assert(text(elements.get('records')).includes('关联作品'));
 assert(text(elements.get('records')).includes('<svg onload="throw 1">planned-b'));
 assert(!elements.get('records').children.some(x=>x.tag==='svg'));
 const reads=calls.filter(x=>x.name==='records'&&x.args.section==='group_thoughts');
 assert.deepEqual(reads.map(x=>[x.args.space_id,x.args.offset]),[['a',0],['a',12],['a',0],['b',0]]);
 assert(reads.every(x=>x.args.work_id===''));
 const experiment=tabs.find(x=>x.dataset.section==='group_inspirations');
 assert(experiment.textContent.includes('实验'));experiment.onclick();await settle();
 assert(text(elements.get('filter')).includes('不作为生产收信'));
 assert(!text(elements.get('records')).includes('synthetic-thought'));
 assert(calls.every(x=>['spaces','summary','records'].includes(x.name)));
 assert.equal(elements.get('error').textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness], check=False, input=json.dumps({"script": script}),
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
