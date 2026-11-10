"""Administrative sharing projection, with temporary SQLite and an isolated DOM."""

from contextlib import closing
from pathlib import Path
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess

import pytest

from image_plugin_under_test.domain import store as ledger
from image_plugin_under_test.domain.policy import Delegation, Scope, StoreError
from image_plugin_under_test.workspace.studio import Studio


OWNER = Scope("synthetic", "stream-a", "owner-a")
OTHER = Scope("synthetic", "stream-b", "owner-b")
ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def view(root, *, group="group-a"):
    return Studio(root, scopes={"a": OWNER}, group_bindings={"a": group} if group else {})


@pytest.fixture
def data(tmp_path):
    store = ledger.ArtistStore(tmp_path / "artist.sqlite3")
    for key, scope, group in (("a", OWNER, "group-a"), ("b", OTHER, "group-b")):
        store.create_space(key, scope, binding_id="synthetic-service")
        version = store.grant(key, scope, Delegation("synthetic-consent", scope.user_id, True))
        # This fixture exercises the projection of persisted policy without
        # depending on the writer's configuration/scheduling entry points.
        store.db.execute("""INSERT INTO group_share_policy
            (space_id,scope_snapshot,platform,stream_id,group_id,mode,auth_version,policy_version)
            VALUES(?,?,?,?,?,'original_group',?,1)""",
                         (key, scope.snapshot(), scope.platform, scope.stream_id, group, version))
    store.close()
    return tmp_path


def test_admin_sharing_uses_only_minimal_matching_policy_without_writes(data, monkeypatch):
    before = snapshot(data)
    monkeypatch.setattr(ledger.ArtistStore, "__init__", lambda *a, **kw: pytest.fail("writer opened"))
    statements = []
    connect = sqlite3.connect

    def trace(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", trace)
    summary = view(data).summary("a")
    assert summary["sharing"] == {"mode": "original_group", "allowed": True}
    assert snapshot(data) == before
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE)\b", sql, re.I)
                   for sql in statements)
    assert all(value not in json.dumps(summary) for value in (OWNER.user_id, OWNER.stream_id,
                                                             OTHER.user_id, "group-b", "policy_version"))


@pytest.mark.parametrize("field,value", [
    ("scope_snapshot", OTHER.snapshot()), ("platform", "other-platform"),
    ("stream_id", "other-stream"), ("group_id", "other-group"), ("auth_version", -1),
])
def test_policy_binding_or_grant_mismatch_denies_without_cross_group_data(data, field, value):
    with closing(sqlite3.connect(data / "artist.sqlite3")) as db, db:
        db.execute(f"UPDATE group_share_policy SET {field}=? WHERE space_id='a'", (value,))
    before = snapshot(data)
    assert view(data).summary("a")["sharing"] == {"mode": "collect_only", "allowed": False}
    assert snapshot(data) == before


def test_collect_only_and_revocation_do_not_hide_historical_authorization(data):
    with closing(sqlite3.connect(data / "artist.sqlite3")) as db, db:
        db.execute("UPDATE group_share_policy SET mode='collect_only' WHERE space_id='a'")
    summary = view(data).summary("a")
    assert summary["authorization"]["authorized"]
    assert summary["sharing"] == {"mode": "collect_only", "allowed": False}
    with closing(sqlite3.connect(data / "artist.sqlite3")) as db, db:
        db.execute("UPDATE group_share_policy SET mode='original_group' WHERE space_id='a'")
        db.execute("UPDATE spaces SET authorized=0 WHERE space_id='a'")
    before = snapshot(data)
    assert view(data).summary("a")["sharing"] == {"mode": "original_group", "allowed": False}
    assert snapshot(data) == before


def test_missing_policy_is_denied_even_when_another_group_is_allowed(data):
    with closing(sqlite3.connect(data / "artist.sqlite3")) as db, db:
        db.execute("DELETE FROM group_share_policy WHERE space_id='a'")
    before = snapshot(data)
    assert view(data).summary("a")["sharing"] == {"mode": "collect_only", "allowed": False}
    assert Studio(data, scopes={"b": OTHER}, group_bindings={"b": "group-b"}).summary("b")["sharing"] == {
        "mode": "original_group", "allowed": True}
    assert snapshot(data) == before


@pytest.mark.parametrize("version", range(1, 9))
def test_actual_legacy_group_schema_defaults_to_collect_only_without_migration(tmp_path, version):
    path = tmp_path / "artist.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for number in range(1, version + 1):
            for statement in getattr(ledger, "_SCHEMA" if number == 1 else f"_SCHEMA_{number}"):
                db.execute(statement)
        db.execute("INSERT INTO spaces(space_id,scope,binding_id,authorized) VALUES(?,?,?,1)",
                   ("a", OWNER.snapshot(), "synthetic-service"))
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(tmp_path)
    assert view(tmp_path).summary("a")["sharing"] == {"mode": "collect_only", "allowed": False}
    assert snapshot(tmp_path) == before


def test_uncreated_group_denies_without_creating_any_directory(tmp_path):
    root = tmp_path / "absent"
    assert view(root).summary("a")["sharing"] == {"mode": "collect_only", "allowed": False}
    assert not root.exists()


def test_private_admin_retains_existing_grant_and_chat_never_reads_group_policy(data, monkeypatch):
    with closing(sqlite3.connect(data / "artist.sqlite3")) as db, db:
        db.execute("DELETE FROM group_share_policy WHERE space_id='a'")
    before = snapshot(data)
    assert view(data, group="").summary("a")["sharing"] == {"mode": "private", "allowed": True}
    statements = []
    connect = sqlite3.connect

    def trace(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", trace)
    summary = Studio(data, scopes={"a": OWNER}).summary("a")
    assert "sharing" not in summary
    assert not any("group_share_policy" in sql.lower() for sql in statements)
    assert snapshot(data) == before


def test_removing_configured_group_binding_does_not_grant_private_permission(data):
    before = snapshot(data)
    assert view(data, group="").summary("a")["sharing"] == {"mode": "collect_only", "allowed": False}
    assert snapshot(data) == before


def test_invalid_ledger_owner_cannot_read_sharing_policy(data):
    before = snapshot(data)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "NOT_FOUND"):
        Studio(data, scopes={"a": OTHER}, group_bindings={"a": "group-a"}).summary("a")
    assert snapshot(data) == before


@pytest.mark.parametrize("mode,allowed,label", [
    ("collect_only", False, "仅收藏"), ("original_group", True, "可自主分享到原群"),
    ("original_group", False, "可自主分享到原群"), ("private", True, "私聊原授权"),
])
@pytest.mark.local_integration
def test_sharing_dom_displays_permission_separately_from_results(mode, allowed, label):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for the isolated Studio DOM harness")
    script = re.search(r"<script>([\s\S]+)</script>", (ROOT / "assets/studio.html").read_text(encoding="utf-8")).group(1)
    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const p=JSON.parse(fs.readFileSync(0,'utf8')),elements=new Map(),calls=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.value='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[];this.append(...items);}
 setAttribute(k,v){this[k]=v;}
 set innerHTML(value){throw new Error('HTML injection sink');}
 addEventListener(){} close(){}
}
const document={createElement:tag=>new Element(tag),getElementById:id=>{
 if(!elements.has(id))elements.set(id,new Element(id==='spaces'?'select':'div'));
 return elements.get(id);
}};
const summary={authorization:{authorized:true,enabled:false},waiting:[],budget:{},counts:{},
 recent_works:[],current_work:null,day:'synthetic-day',coverage:'synthetic-coverage',
 sharing:{mode:p.mode,allowed:p.allowed}};
const maibot={ready:Promise.resolve(),async query(name,args){calls.push({name,args});
 if(name==='spaces')return {items:[{space_id:'a'}]};if(name==='summary')return summary;
 assert.equal(name,'records');return {items:[],available:true,has_more:false,next_offset:null};
}};
const text=el=>el.textContent+' '+el.children.map(text).join(' ');
vm.runInNewContext(p.script,{document,maibot,URL:{revokeObjectURL(){}}});
(async()=>{for(let i=0;i<15;i++)await new Promise(setImmediate);
 const output=text(elements.get('summary'));
 assert(output.includes('自主分享权限'));assert(output.includes(p.label));
 assert(output.includes('允许不等于必发'));assert(output.includes('实际分享结果以作品记录为准'));
 assert(output.includes(p.allowed?'是否分享由 MaiBot 自主决定':'当前不允许自主分享'));
 assert(!output.includes('已发送'));assert.equal(elements.get('error').textContent,'');
 assert(calls.every(x=>['spaces','summary','records'].includes(x.name)));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness], check=False,
                            input=json.dumps({"script": script, "mode": mode, "allowed": allowed, "label": label}),
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
