"""Genuine Host schema/registry/gateway + SDK, with isolated dispatch only.

Public source files are loaded without Host bootstrap, configuration or runtime.
The JavaScript test executes the real iframe SDK in a DOM/message-port harness;
it does not control a browser and does not claim visual/browser acceptance.

External contracts are opt-in:
    CODEX_IMAGE_TEST_HOST_SOURCE=<public Host src directory>
    CODEX_IMAGE_TEST_DASHBOARD_SOURCE=<public Dashboard directory containing src/>
Optional CODEX_IMAGE_TEST_NODE=<node executable>; otherwise use Node on PATH.
Missing opt-in or Node skips only the dependent contract tests. Explicit invalid
paths fail. Default plugin tests require neither workspace layout nor a browser.
"""

from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
import ast
import asyncio
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from maibot_sdk.components import collect_components
from pydantic import Field, model_validator
from starlette.exceptions import HTTPException
import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.policy import StoreError
from test_studio import OWNER, data as _studio_data


ROOT = Path(__file__).resolve().parents[1]
HOST_ID = "pargosaks.maibot-codex-image"
data = _studio_data  # Export the same isolated fixture; do not duplicate its setup.


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def definitions(path, names, namespace):
    nodes = [node for node in ast.parse(path.read_text(encoding="utf-8")).body
             if getattr(node, "name", None) in names]
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


@pytest.fixture
def host_source():
    source = os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE")
    if source is None:
        pytest.skip("Opt in with CODEX_IMAGE_TEST_HOST_SOURCE=<public Host src directory>")
    root = Path(source)
    expected = ("plugin_runtime/webui_schema.py", "plugin_runtime/protocol/errors.py",
                "plugin_runtime/host/component_timeout.py", "plugin_runtime/host/api_registry.py",
                "webui/routers/plugin/webui_extensions.py")
    if not source.strip() or any(not (root / item).is_file() for item in expected):
        pytest.fail("Explicit CODEX_IMAGE_TEST_HOST_SOURCE does not contain the required public Host files")
    return root


@pytest.fixture
def dashboard_source():
    source = os.environ.get("CODEX_IMAGE_TEST_DASHBOARD_SOURCE")
    if source is None:
        pytest.skip("Opt in with CODEX_IMAGE_TEST_DASHBOARD_SOURCE=<public Dashboard directory>")
    root = Path(source)
    expected = ("src/lib/plugin-custom-page.ts", "src/components/plugin-custom-page.tsx")
    if not source.strip() or any(not (root / item).is_file() for item in expected):
        pytest.fail("Explicit CODEX_IMAGE_TEST_DASHBOARD_SOURCE does not contain the required public Dashboard files")
    return root


@pytest.mark.parametrize("name,fixture", [("CODEX_IMAGE_TEST_HOST_SOURCE", host_source),
                                        ("CODEX_IMAGE_TEST_DASHBOARD_SOURCE", dashboard_source)])
def test_external_contract_paths_are_opt_in_and_explicit_errors_fail(monkeypatch, tmp_path, name, fixture):
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(pytest.skip.Exception, match="Opt in"):
        fixture.__wrapped__()
    monkeypatch.setenv(name, str(tmp_path / "missing-public-source"))
    with pytest.raises(pytest.fail.Exception, match="Explicit"):
        fixture.__wrapped__()
    monkeypatch.setenv(name, "")
    with pytest.raises(pytest.fail.Exception, match="Explicit"):
        fixture.__wrapped__()


@pytest.fixture
def host(host_source):
    schema = load_file("_studio_public_schema", host_source / "plugin_runtime/webui_schema.py")
    errors = load_file("_studio_public_rpc_errors", host_source / "plugin_runtime/protocol/errors.py")
    module = ModuleType("_studio_public_gateway")
    sys.modules[module.__name__] = module
    ns = module.__dict__
    ns.update(Any=Any, Dict=Dict, List=List, Literal=Literal, Optional=Optional, Set=Set, Tuple=Tuple,
              dataclass=dataclass, field=field, logger=logging.getLogger("isolated-studio-host"),
              asyncio=asyncio, HTTPException=HTTPException, Field=Field, model_validator=model_validator,
              ErrorCode=errors.ErrorCode, RPCError=errors.RPCError,
              StrictModel=schema.StrictModel, validate_json=schema.validate_json, _inflight={})
    definitions(host_source / "plugin_runtime/host/component_timeout.py", {"normalize_component_timeout_ms"}, ns)
    definitions(host_source / "plugin_runtime/host/api_registry.py", {"APIEntry", "APIRegistry"}, ns)
    definitions(host_source / "webui/routers/plugin/webui_extensions.py",
                {"Invocation", "_validate_result", "_invoke"}, ns)
    module.schema = schema
    return module


def test_real_schema_loads_workspace_and_strict_bindings(host):
    extension = host.schema.load_webui_extension(str(ROOT))
    assert extension.workspace_title == "小画师"
    page = extension.pages[0]
    assert page.placement == "workspace" and page.entry == "studio.html"
    assert not page.actions and not page.uploads
    assert "<h1>小画师</h1>" in page.document
    page.queries["records"].validate_args({"space_id": "a", "section": "tasks", "limit": 50})
    for args in ({"space_id": "a", "section": "tasks", "limit": 51},
                 {"space_id": "a", "section": "tasks", "scope": {}},
                 {"space_id": "a", "section": "private_table"}):
        with pytest.raises(ValueError):
            page.queries["records"].validate_args(args)
    with pytest.raises(ValueError):
        page.queries["image"].validate_args({"space_id": "a", "version_id": "v", "thumbnail": "false"})


async def test_real_sdk_private_components_and_host_gateway_reject_forgery(host, data):
    root, _, view = data
    wrappers, registry = CodexImagePlugin(), host.APIRegistry()
    config = wrappers.build_default_config()
    config["autonomy"]["spaces"] = [{"space_id": "a", "platform": OWNER.platform,
                                        "stream_id": OWNER.stream_id, "user_id": OWNER.user_id}]
    wrappers.set_plugin_config(config)
    wrappers._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=root)))
    assert importlib.metadata.version("maibot-plugin-sdk") == "2.16.0"
    components = [component for component in collect_components(wrappers) if component["type"] == "API"]
    assert len(components) == 4
    for component in components:
        assert component["metadata"]["public"] is False
        registry.register_api(component["name"], HOST_ID, component["metadata"])
    async def invoke(plugin_id, handler_name, args, timeout_ms):
        assert plugin_id == HOST_ID and timeout_ms == 10000
        result = await getattr(wrappers, handler_name)(**args)
        return SimpleNamespace(error=None, payload={"success": True, "result": result})
    extension = host.schema.load_webui_extension(str(ROOT))
    supervisor = SimpleNamespace(api_registry=registry, invoke_api=invoke)
    host._extensions = lambda: {HOST_ID: (supervisor, extension)}
    result = await host._invoke(HOST_ID, "studio", "queries", "summary", host.Invocation(args={"space_id": "a"}))
    assert result["result"]["recent_works"][0]["generated"] is True
    for plugin_id, kind, name, args, status in (
        ("other.plugin", "queries", "summary", {"space_id": "a"}, 404),
        (HOST_ID, "actions", "summary", {"space_id": "a"}, 403),
        (HOST_ID, "queries", "summary", {"space_id": "a", "scope": {}}, 422),
        (HOST_ID, "queries", "summary", {"space_id": "b"}, 502),
    ):
        with pytest.raises(HTTPException) as exc:
            await host._invoke(plugin_id, "studio", kind, name, host.Invocation(args=args))
        assert exc.value.status_code == status
    host._validate_result(view.image("a", "v-a", False))
    assert not host._inflight
    snapshot = await wrappers._studio_chat_read("a", OWNER)
    assert snapshot.data["recent_works"][0]["generated"] is True
    with pytest.raises(StoreError, check=lambda exc: exc.code == "NOT_FOUND"):
        await wrappers._studio_chat_read("b", OWNER)
    assert wrappers.artist_runtime is None and wrappers.journal is None and wrappers.bridge is None


def test_actual_iframe_csp_allows_in_frame_blob_images(dashboard_source):
    bridge = (dashboard_source / "src/lib/plugin-custom-page.ts").read_text(encoding="utf-8")
    renderer = (dashboard_source / "src/components/plugin-custom-page.tsx").read_text(encoding="utf-8")
    assert "img-src data: blob:" in bridge
    assert "connect-src 'none'" in bridge
    assert 'sandbox="allow-scripts"' in renderer
    # Original Blob URLs are created in the iframe's own origin, never by parent.
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    assert "URL.createObjectURL(new Blob" in html and "URL.revokeObjectURL" in html
    assert "innerHTML" not in html and "fetch(" not in html and "JSON.stringify(item" not in html


@pytest.mark.parametrize("scenario", ["happy", "spaces_retry", "space_summary_failure", "records_failure",
                                      "catalog_labels", "discard_review"])
def test_real_iframe_sdk_queries_and_chinese_page_without_browser(data, dashboard_source, scenario):
    explicit_node = os.environ.get("CODEX_IMAGE_TEST_NODE")
    if explicit_node is not None:
        node = shutil.which(explicit_node) if explicit_node.strip() else None
        if not node:
            pytest.fail("Explicit CODEX_IMAGE_TEST_NODE is not an executable Node path")
    else:
        node = shutil.which("node")
        if not node:
            pytest.skip("Node not found; put Node on PATH or set CODEX_IMAGE_TEST_NODE")
    _, db, view = data
    if scenario == "discard_review":
        db.db.execute("UPDATE reviews SET review=? WHERE space_id='a'",
                      (json.dumps({"decision": "discard", "reason": "synthetic discard", "strengths": [], "issues": []}),))
    bridge = (dashboard_source / "src/lib/plugin-custom-page.ts").read_text(encoding="utf-8")
    sdk = re.search(r"const SDK = `([\s\S]+?)`\n", bridge).group(1).replace("__MAIBOT_NONCE__", '"isolated-test"')
    html = (ROOT / "studio.html").read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]+)</script>", html).group(1)
    payload = {"scenario": scenario, "sdk": sdk, "script": script, "summary": view.summary("a"), "works": view.records("a", "works"),
               "versions": view.records("a", "versions"), "thumbnail": view.image("a", "v-a"),
               "original": view.image("a", "v-a", False), "reviews": view.records("a", "reviews")}
    harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict'),fs=require('node:fs');
const p=JSON.parse(fs.readFileSync(0,'utf8')),elements=new Map(),calls=[];
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.style={setProperty(){}};this.classList={toggle(){}};this.value='';this.open=false;}
  append(...items){this.children.push(...items);if(this.tag==='select'&&!this.value&&items.length)this.value=items[0].value;for(const i of items)i.isConnected=true;}
  prepend(...items){this.children.unshift(...items);}
  replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(k,v){this[k]=v;}
  addEventListener(){}
  remove(){}
  close(){this.open=false;}
  showModal(){this.open=true;}
}
const listeners={},doc={createElement:tag=>new Element(tag),getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id==='spaces'?'select':id==='imageDialog'?'dialog':'div'));return elements.get(id);},head:new Element('head'),body:new Element('body'),documentElement:new Element('html'),readyState:'complete'};
let failed=false;
const port={postMessage(request){calls.push(request);let result;
  const a=request.args, mode=p.scenario;
  if(!failed&&((mode==='spaces_retry'&&request.name==='spaces')||(mode==='space_summary_failure'&&request.name==='summary'&&a.space_id==='b')||(mode==='records_failure'&&request.name==='records'&&a.offset===12))){failed=true;queueMicrotask(()=>port.onmessage({data:{id:request.id,error:'synthetic read denied'}}));return;}
  if(request.name==='spaces')result={items:[{space_id:'a'},{space_id:'b'}]};
  else if(request.name==='summary')result={...p.summary,space_id:a.space_id};
  else if(request.name==='records'){
    result=a.section==='versions'?p.versions:a.section==='reviews'?p.reviews:p.works;
    if(a.section==='works'&&a.offset===0&&['space_summary_failure','records_failure'].includes(mode))result={...p.works,has_more:true,next_offset:12};
    if(a.section==='profiles')result={items:[{profile_id:'profile',revision:1,active:1,data:{title:'合成档案'},provenance:{origin:'configured',source_ref:'host-config'}}],has_more:false,next_offset:null};
    if(a.section==='series')result={items:[{series_id:'series',active:1,closed:0,provenance:{origin:'autonomous'}}],has_more:false,next_offset:null};
    if(a.section==='assets')result={items:[{asset_id:'asset',roles:['palette','style','character','composition'],source_kind:'authorized_chat'}],has_more:false,next_offset:null};
  }
  else if(request.name==='image')result=request.args.thumbnail?p.thumbnail:p.original;
  else throw new Error('Unexpected binding');
  queueMicrotask(()=>port.onmessage({data:{id:request.id,result}}));
}};
const parent={postMessage(){queueMicrotask(()=>listeners.message({source:parent,data:{type:'maibot:connect',page:{layout:'viewport'},context:{theme:'light',language:'zh-CN',styles:''}},ports:[port]}));}};
let revoked=0;
const ctx={document:doc,parent,console,queueMicrotask,setTimeout,clearTimeout,Blob,Uint8Array,atob,URL:{createObjectURL:()=> 'blob:opaque-origin/test',revokeObjectURL:()=>revoked++},addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener(){},dispatchEvent(){}};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(p.sdk,ctx);vm.runInContext(p.script,ctx);
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(setImmediate);};
const text=el=>el.textContent+' '+el.children.map(text).join(' ');
(async()=>{
 await settle();
 if(p.scenario==='spaces_retry'){
   assert(elements.get('error').textContent.includes('denied'));assert(elements.get('next').disabled);
   elements.get('refresh').onclick();await settle();
   assert.equal(calls.filter(x=>x.name==='spaces').length,2);assert(calls.some(x=>x.name==='summary'));assert(calls.some(x=>x.name==='records'));assert.equal(elements.get('error').textContent,'');return;
 }
 assert.equal(elements.get('error').textContent,'');
 assert.deepEqual(calls.slice(0,3).map(x=>x.name),['spaces','summary','records']);
 const summary=text(elements.get('summary'));assert(summary.includes('private-subject'));assert(summary.includes('尚无分享记录'));assert(summary.includes('当前作品'));
 assert(text(elements.get('records')).includes('已有成图'));
 if(p.scenario==='space_summary_failure'){
   assert(!elements.get('next').disabled);elements.get('spaces').value='b';elements.get('spaces').onchange();await settle();
   assert(elements.get('next').disabled&&elements.get('prev').disabled);assert.equal(elements.get('page').textContent,'');assert.equal(elements.get('records').children.length,0);
   elements.get('refresh').onclick();await settle();assert.equal(elements.get('error').textContent,'');
   const reads=calls.filter(x=>x.name==='records'&&x.args.space_id==='b');assert(reads.length>0);assert(reads.every(x=>x.args.offset===0));assert.equal(calls.filter(x=>x.name==='spaces').length,3);return;
 }
 if(p.scenario==='records_failure'){
   elements.get('next').onclick();await settle();assert(elements.get('error').textContent.includes('denied'));
   assert.equal(elements.get('page').textContent,'');assert.equal(elements.get('records').children.length,0);assert(elements.get('next').disabled);
   const spacesBefore=calls.filter(x=>x.name==='spaces').length;elements.get('refresh').onclick();await settle();
   assert(calls.filter(x=>x.name==='spaces').length>spacesBefore);assert.equal(elements.get('error').textContent,'');assert(elements.get('page').textContent.includes('第 13 条'));return;
 }
 if(p.scenario==='catalog_labels'){
   elements.get('tabs').children[4].onclick();await settle();assert(text(elements.get('records')).includes('配置导入'));assert(!text(elements.get('records')).includes('configured'));
   elements.get('tabs').children[5].onclick();await settle();assert(text(elements.get('records')).includes('自主积累'));
   elements.get('tabs').children[6].onclick();await settle();const body=text(elements.get('records'));for(const label of ['配色','风格','角色','构图','已授权聊天图片'])assert(body.includes(label));assert(!body.includes('palette'));return;
 }
 if(p.scenario==='discard_review'){
   assert(summary.includes('弃稿'));elements.get('tabs').children[3].onclick();await settle();assert(text(elements.get('records')).includes('弃稿'));assert(!text(elements.get('records')).includes('未知状态'));return;
 }
 elements.get('tabs').children[2].onclick();await settle();
 assert(calls.some(x=>x.name==='image'&&x.args.thumbnail===true));
 const card=elements.get('records').children[0];card.children.find(x=>x.tag==='button'&&x.textContent==='打开原图').onclick();await settle();
 assert(elements.get('imageDialog').open);assert(calls.some(x=>x.name==='image'&&x.args.thumbnail===false));
 elements.get('closeImage').onclick();assert.equal(revoked,1);
 assert(calls.every(x=>x.kind==='queries'));assert.equal(elements.get('error').textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness], check=False, input=json.dumps(payload, ensure_ascii=True),
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
