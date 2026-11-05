"""Read-only group Studio projection; synthetic state and a JS DOM harness only."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.inspiration.group_notebook import GroupNotebook
from image_plugin_under_test.domain.policy import Scope, StoreError
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.workspace.studio import Studio


ROOT = Path(__file__).resolve().parents[1]
OWNER = Scope("synthetic", "group-stream-a", "delegator")
OTHER = Scope("synthetic", "group-stream-b", "other-delegator")
START = datetime(2026, 11, 1, 1, tzinfo=timezone.utc).timestamp()


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def group_data(tmp_path):
    store = ArtistStore(tmp_path / "artist.sqlite3")
    store.create_space("a", OWNER, binding_id="synthetic-service")
    store.create_space("b", OTHER, binding_id="synthetic-service")
    store.close()
    now = [START]
    notebook = GroupNotebook(tmp_path, clock=lambda: now[0])
    try:
        for key, scope in (("a", OWNER), ("b", OTHER)):
            notebook.bind(space_id=key, platform=scope.platform, group_id="group-" + key,
                          stream_id=scope.stream_id, owner_id=scope.user_id)
        for index, theme in enumerate(("下雨", "山林", "便利店")):
            for message in range(3):
                result = notebook.observe(space_id="a", platform=OWNER.platform, group_id="group-a",
                                          stream_id=OWNER.stream_id, owner_id=OWNER.user_id,
                                          member_id="private-member-" + str(message % 2),
                                          message_id=f"private-message-{index}-{message}",
                                          text=theme + " private-original-text")
                assert result.accepted
            now[0] += 601
            notebook.flush()
        assert notebook.observe(space_id="b", platform=OTHER.platform, group_id="group-b",
                                stream_id=OTHER.stream_id, owner_id=OTHER.user_id,
                                member_id="foreign-member", message_id="foreign-message",
                                text="便利店 foreign-original").accepted
        now[0] += 601
        notebook.flush()
    finally:
        notebook.close()
    return tmp_path, tmp_path / "group_notebook.sqlite3"


def reader(root, *, scope=OWNER, group_id="group-a"):
    return Studio(root, scopes={"a": scope}, group_bindings={"a": group_id}, enabled=False)


def test_group_read_is_scoped_minimal_paginated_and_changes_no_bytes(group_data, monkeypatch):
    root, _ = group_data
    before = snapshot(root)
    monkeypatch.setattr(ArtistStore, "__init__", lambda *a, **kw: pytest.fail("mutating ledger opened"))
    monkeypatch.setattr(GroupNotebook, "__init__", lambda *a, **kw: pytest.fail("mutating notebook opened"))
    view = reader(root)
    first = view.records("a", "group_inspirations", limit=2)
    assert first["available"] and first["has_more"] and first["next_offset"] == 2
    assert [item["inspiration_id"] for item in first["items"]] == ["store", "forest"]
    last_time = datetime.fromtimestamp(START + 1202, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert first["items"][0] == {
        "inspiration_id": "store", "summary": "便利店", "participant_count": 2, "message_count": 3,
        "created_at": last_time, "updated_at": last_time,
        "adoption_status": "unadopted"}
    second = view.records("a", "group_inspirations", offset=first["next_offset"], limit=2)
    assert [item["inspiration_id"] for item in second["items"]] == ["rain"]
    assert second["next_offset"] is None and not second["has_more"]
    assert view.records("a", "group_inspirations", offset=10000)["items"] == []
    assert view.records("a", "group_inspirations", limit=2) == first
    assert snapshot(root) == before
    text = json.dumps(first) + json.dumps(second)
    assert all(secret not in text for secret in ("private-", "foreign-", OWNER.user_id, OWNER.stream_id))


@pytest.mark.parametrize("scope", [OTHER, Scope("other-platform", OWNER.stream_id, OWNER.user_id),
                                  Scope(OWNER.platform, "other-stream", OWNER.user_id),
                                  Scope(OWNER.platform, OWNER.stream_id, "other-owner")])
def test_group_read_checks_persisted_ledger_owner(group_data, scope):
    root, _ = group_data
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root, scope=scope).records("a", "group_inspirations")
    assert snapshot(root) == before


@pytest.mark.parametrize("field,value", [("owner_id", "other-owner"), ("platform", "other-platform"),
                                        ("stream_id", "other-stream"), ("group_id", "other-group")])
def test_notebook_binding_checked_even_when_ledger_matches_reader(group_data, field, value):
    root, _ = group_data
    scope = Scope(value if field == "platform" else OWNER.platform,
                  value if field == "stream_id" else OWNER.stream_id,
                  value if field == "owner_id" else OWNER.user_id)
    group_id = value if field == "group_id" else "group-a"
    # Simulate a replaced ledger/config while preserving the notebook's real,
    # immutable binding. Matching ledger authority alone must not expose it.
    with closing(sqlite3.connect(root / "artist.sqlite3")) as db, db:
        db.execute("UPDATE spaces SET scope=? WHERE space_id='a'", (scope.snapshot(),))
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root, scope=scope, group_id=group_id).records("a", "group_inspirations")
    assert snapshot(root) == before


def test_chat_scoped_projection_cannot_read_group_notebook(group_data):
    root, _ = group_data
    before = snapshot(root)
    assert Studio(root, scopes={"a": OWNER}).records("a", "group_inspirations")["available"] is False
    with error("NOT_FOUND"):
        reader(root, group_id="other-group").records("a", "group_inspirations")
    assert snapshot(root) == before


def test_missing_notebook_does_not_create_it(group_data):
    root, path = group_data
    path.unlink()  # Synthetic fixture only; the production reader never deletes.
    before = snapshot(root)
    assert reader(root).records("a", "group_inspirations")["available"] is False
    assert snapshot(root) == before


def test_notebook_without_persisted_artist_owner_is_unavailable(group_data):
    root, _ = group_data
    (root / "artist.sqlite3").unlink()
    before = snapshot(root)
    assert reader(root).records("a", "group_inspirations")["available"] is False
    assert snapshot(root) == before


def test_orphaned_topics_do_not_grant_read_authority(group_data):
    root, path = group_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM notebook_groups WHERE space_id='a'")
    before = snapshot(root)
    with error("NOT_FOUND"):
        reader(root).records("a", "group_inspirations")
    assert snapshot(root) == before


@pytest.mark.parametrize("version", [0, 2, 999])
def test_unknown_notebook_schema_is_rejected_without_migration(group_data, version):
    root, path = group_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(root)
    with error("SCHEMA_UNSUPPORTED"):
        reader(root).records("a", "group_inspirations")
    assert snapshot(root) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_linked_notebook_and_sidecars_are_rejected_before_attach(group_data, monkeypatch, suffix):
    root, _ = group_data
    original = Path.is_symlink

    def linked(path):
        return path.name == "group_notebook.sqlite3" + suffix or original(path)

    monkeypatch.setattr(Path, "is_symlink", linked)
    before = snapshot(root)
    with error("UNSAFE_STATE_PATH"):
        reader(root).records("a", "group_inspirations")
    assert snapshot(root) == before


def test_group_read_bounds_text_without_changing_notebook_schema(group_data):
    root, path = group_data
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE notebook_topics SET title=? WHERE space_id='a'", ("鲸" * 20000,))
    page = reader(root).records("a", "group_inspirations")
    for item in page["items"]:
        assert len(item["summary"]) == 1000
        assert len(item["created_at"]) == 20 and len(item["updated_at"]) == 20
        assert item["adoption_status"] == "unadopted"


def test_group_read_never_fetches_members_original_text_or_opens_writable_database(group_data, monkeypatch):
    root, _ = group_data
    connect = sqlite3.connect
    reads, statements = [], []

    def readonly_connect(path, *args, **kwargs):
        assert "mode=ro" in str(path) and kwargs["uri"] is True
        db = connect(path, *args, **kwargs)
        db.set_trace_callback(statements.append)

        def authorize(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_READ:
                reads.append((database, arg1, arg2))
                assert arg1 not in {"notebook_seen", "notebook_topic_members"}
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    assert reader(root).records("a", "group_inspirations")["items"]
    assert any(database == "notebook" for database, _, _ in reads)
    assert all("mode=ro" in sql for sql in statements if sql.startswith("ATTACH"))
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)


async def test_private_plugin_wrapper_reads_without_runtime_or_dispatch(group_data, monkeypatch):
    root, _ = group_data
    plugin = CodexImagePlugin()
    config = plugin.build_default_config()
    config["autonomy"]["spaces"] = [{"space_id": "a", "platform": OWNER.platform, "stream_id": OWNER.stream_id,
                                     "user_id": OWNER.user_id, "group_id": "group-a"}]
    plugin.set_plugin_config(config)
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=root)))
    monkeypatch.setattr(ArtistStore, "__init__", lambda *a, **kw: pytest.fail("writer opened"))
    monkeypatch.setattr(GroupNotebook, "__init__", lambda *a, **kw: pytest.fail("notebook writer opened"))
    before = snapshot(root)
    page = await plugin.studio_records("a", "group_inspirations", limit=1)
    assert page["items"][0]["inspiration_id"] == "store"
    assert plugin.artist_runtime is None and plugin.journal is None and plugin.bridge is None
    assert snapshot(root) == before


@pytest.mark.parametrize("bindings", [{}, {"a": "group-a"}])
def test_missing_state_is_unavailable_without_creating_directory(tmp_path, bindings):
    root = tmp_path / "absent"
    reader = Studio(root, scopes={"a": OWNER}, group_bindings=bindings)
    page = reader.records("a", "group_inspirations")
    assert page == {"items": [], "offset": 0, "limit": 20, "has_more": False,
                    "next_offset": None, "available": False}
    assert not root.exists()


@pytest.mark.parametrize("bindings", [[], {"foreign": "group-a"}, {"a": ""}, {"a": "*"}, {"a": "x" * 201}])
def test_group_read_binding_requires_trusted_explicit_scope(tmp_path, bindings):
    with pytest.raises(StoreError):
        Studio(tmp_path, scopes={"a": OWNER}, group_bindings=bindings)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("offset,limit", [(-1, 1), (10001, 1), (True, 1), (0, 0), (0, 51), (0, True)])
def test_group_pagination_rejects_invalid_arguments_before_io(tmp_path, monkeypatch, offset, limit):
    reader = Studio(tmp_path, scopes={"a": OWNER}, group_bindings={"a": "group-a"})
    monkeypatch.setattr(reader, "_open", lambda: pytest.fail("invalid query accessed storage"))
    with error("INVALID_ARGUMENT"):
        reader.records("a", "group_inspirations", offset=offset, limit=limit)


def test_group_query_rejects_other_space_and_work_filter_before_io(tmp_path, monkeypatch):
    reader = Studio(tmp_path, scopes={"a": OWNER}, group_bindings={"a": "group-a"})
    monkeypatch.setattr(reader, "_open", lambda: pytest.fail("invalid query accessed storage"))
    with error("NOT_FOUND"):
        reader.records("foreign", "group_inspirations")
    with error("INVALID_ARGUMENT"):
        reader.records("a", "group_inspirations", work_id="work")


def test_webui_reuses_private_records_query_and_has_no_actions():
    page = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))["pages"][0]
    records = page["queries"]["records"]
    assert records["api"] == "studio_records" and records["version"] == "1"
    assert "group_inspirations" in records["parameters"]["section"]["choices"]
    assert set(page["queries"]) == {"spaces", "summary", "records", "image"}
    assert not page["actions"] and not page["uploads"]


def test_group_page_pagination_and_safe_text_without_browser():
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
 addEventListener(){}
 close(){}
}
const document={createElement:tag=>new Element(tag),getElementById:id=>{
 if(!elements.has(id))elements.set(id,new Element(id==='spaces'?'select':'div'));
 return elements.get(id);
}};
const summary={authorization:{authorized:false,enabled:false},waiting:[],budget:{},counts:{},
 recent_works:[],current_work:null,day:'synthetic-day',coverage:'synthetic-coverage'};
const item={summary:'<img src=x onerror="throw 1">synthetic-theme',participant_count:3,message_count:7,adoption_status:'unadopted',
 created_at:'2026-11-01T01:00:00+00:00',updated_at:'2026-11-02T02:00:00+00:00',
 members:['private-member'],raw_text:'private-original-text',source_ref:'private-message-id'};
const maibot={ready:Promise.resolve(),async query(name,args){
 calls.push({name,args});
 if(name==='spaces')return {items:[{space_id:'a'},{space_id:'b'}]};
 if(name==='summary')return summary;
 assert.equal(name,'records');
 if(args.section!=='group_inspirations')return {items:[],available:true,has_more:false,next_offset:null};
 return {items:[{...item,summary:item.summary+':'+args.space_id+':'+args.offset}],available:true,
 has_more:args.offset===0,next_offset:args.offset===0?12:null};
}};
vm.runInNewContext(p.script,{document,maibot,URL:{revokeObjectURL(){}}});
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(setImmediate);};
const text=el=>el.textContent+' '+el.children.map(text).join(' ');
(async()=>{
 await settle();
 const tab=elements.get('tabs').children.find(x=>x.dataset.section==='group_inspirations');
 assert(tab);tab.onclick();await settle();
 assert.equal(elements.get('error').textContent,'');
 assert(text(elements.get('filter')).includes('Host 缺少最终过滤通过凭据'));
 const body=text(elements.get('records'));
 for(const label of ['主题摘要','参与人数','消息数','首次记录','最近更新','未采用',item.summary])assert(body.includes(label));
 for(const secret of ['private-member','private-original-text','private-message-id'])assert(!body.includes(secret));
 assert(!elements.get('next').disabled);
 elements.get('next').onclick();await settle();
 assert(text(elements.get('records')).includes(':a:12'));assert(elements.get('next').disabled);
 elements.get('prev').onclick();await settle();assert(text(elements.get('records')).includes(':a:0'));
 elements.get('spaces').value='b';elements.get('spaces').onchange();await settle();
 assert(text(elements.get('records')).includes(':b:0'));assert(!text(elements.get('records')).includes(':a:'));
 const reads=calls.filter(x=>x.name==='records'&&x.args.section==='group_inspirations');
 assert.deepEqual(reads.map(x=>[x.args.space_id,x.args.offset]),[['a',0],['a',12],['a',0],['b',0]]);
 assert(reads.every(x=>x.args.work_id===''));assert(!calls.some(x=>x.name==='image'));
 assert.equal(elements.get('error').textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness], check=False, input=json.dumps({"script": script}),
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
