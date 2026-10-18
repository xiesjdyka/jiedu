import json
import os
import subprocess
import sys
import tempfile
import unittest
import inspect
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from knowledge_bridge import ai_access, client_config, local_ai, mcp_audit, mcp_server
from knowledge_bridge.client_access import (
    ClientAccessGate, ClientIdentityRegistry, ClientPermissionService,
    KnowledgeScope, PolicyDecision, resolve_document_target,
)
from knowledge_bridge.workspace import initialize_workspace


class EvidenceGateTests(unittest.TestCase):
    def harness(self, base: Path):
        context=initialize_workspace(base/"workspace")
        files={
            "00_系统/formal.md":"formal needle",
            "90_原始档案/evidence.md":"evidence needle",
            "99_收件箱/inbox.md":"inbox needle",
        }
        for relative,content in files.items():
            path=context.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,"utf-8")
        registry=ClientIdentityRegistry(context,base/"machine")
        created=registry.create("codex","Fictional Codex","codex","stdio")
        identity=registry.authenticate("codex",created["secret"])
        permissions=ClientPermissionService(registry)
        for scope,operation,decision in (
            ("formal","search","allow"),("formal","read","allow"),
            ("evidence","search","ask"),("evidence","read","ask"),
            ("inbox","read","deny"),("protected","read_protected","deny"),
        ):permissions.set_policy(identity.internal_id,scope,operation,decision)
        return context,registry,permissions,identity,ClientAccessGate(permissions,identity)

    @staticmethod
    def confirm(service: ClientPermissionService):
        pending=service.pending();assert pending
        service.confirm(pending[-1]["approval_id"])

    def mcp_patches(self, identity, permissions, gate):
        return patch.multiple(
            mcp_server,_IDENTITY=identity,_PERMISSIONS=permissions,_GATE=gate,
            audited=lambda *args,**kwargs:nullcontext(),record_call=Mock(),_object_broker=Mock(side_effect=PermissionError('isolated ungranted inbox')),
        )

    def test_formal_and_raw_search_are_separate_and_query_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            context,_,permissions,identity,gate=self.harness(Path(temporary))
            with patch.object(ai_access,"load_knowledge_base",return_value={"vault_path":str(context.root)}), \
                 patch.object(ai_access,"markdown_files",return_value=["00_系统/formal.md","90_原始档案/evidence.md","99_收件箱/inbox.md"]):
                formal=ai_access.knowledge_search("needle",include_raw=False)
                raw=ai_access.knowledge_search("needle",include_raw=True)
            self.assertEqual(["00_系统/formal.md"],[x["source_path"] for x in formal["results"]])
            self.assertEqual(["90_原始档案/evidence.md"],[x["source_path"] for x in raw["results"]])
            read=Mock(side_effect=lambda query,limit,include_raw,*args,**kwargs:{"results":[{
                "source_path":"90_原始档案/evidence.md" if include_raw else "00_系统/formal.md"}]})
            with self.mcp_patches(identity,permissions,gate),patch.object(mcp_server,"direct_knowledge_search",read):
                self.assertEqual("00_系统/formal.md",mcp_server.knowledge_search("formal",include_raw=False)["results"][0]["source_path"])
                pending=mcp_server.knowledge_search("needle",limit=7,include_raw=True)
                self.assertEqual("owner_confirmation_required",pending["status"]);self.assertEqual(1,read.call_count)
                self.assertNotIn("approval_token",pending)
                self.confirm(permissions)
                self.assertEqual("owner_confirmation_required",mcp_server.knowledge_search("changed",limit=7,include_raw=True)["status"])
                allowed=mcp_server.knowledge_search("needle",limit=7,include_raw=True)
                self.assertEqual("90_原始档案/evidence.md",allowed["results"][0]["source_path"])
                self.assertEqual("owner_confirmation_required",mcp_server.knowledge_search("needle",limit=7,include_raw=True)["status"])

    def test_document_scope_path_safety_and_chunk_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            context,_,permissions,identity,gate=self.harness(Path(temporary))
            self.assertEqual(KnowledgeScope.FORMAL,resolve_document_target(context,"00_系统/formal.md").scope)
            self.assertEqual(KnowledgeScope.EVIDENCE,resolve_document_target(context,"90_原始档案/evidence.md").scope)
            self.assertEqual(KnowledgeScope.INBOX,resolve_document_target(context,"99_收件箱/inbox.md").scope)
            for invalid in ("../outside.md",str((context.root/"00_系统/formal.md").resolve()),".jiedu/workspace.json"):
                with self.assertRaises(ValueError):resolve_document_target(context,invalid)
            protected=context.protected_root/"hidden.md";protected.parent.mkdir(parents=True);protected.write_text("hidden","utf-8")
            with self.assertRaises(ValueError):resolve_document_target(context,".jiedu/protected/hidden.md")
            reader=Mock(return_value={"source_path":"fixture"})
            with self.mcp_patches(identity,permissions,gate),patch.object(mcp_server,"doc",reader):
                mcp_server.get_document("00_系统/formal.md");self.assertEqual(1,reader.call_count)
                with self.assertRaises(PermissionError):mcp_server.get_document("99_收件箱/inbox.md")
                pending=mcp_server.get_document("90_原始档案/evidence.md","chunk-a")
                self.assertEqual("owner_confirmation_required",pending["status"]);self.assertEqual(1,reader.call_count)
                self.confirm(permissions)
                self.assertEqual("owner_confirmation_required",mcp_server.get_document("90_原始档案/evidence.md","chunk-b")["status"])
                mcp_server.get_document("90_原始档案/evidence.md","chunk-a")
                self.assertEqual(2,reader.call_count)

    def test_raw_message_and_session_archive_bind_exact_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,_,permissions,identity,gate=self.harness(Path(temporary))
            raw=Mock(return_value={"message_id":"m1","content":"fictional evidence"})
            archive=Mock(return_value={"session_id":"s1","messages":[{"content":"fictional evidence"}],
                                       "page":{"total":1,"has_earlier":False,"next_before":None}})
            listing=Mock(return_value={"items":[{"session_id":"s1"}],"total":1,"has_more":False})
            with self.mcp_patches(identity,permissions,gate), \
                 patch.object(mcp_server,"raw_message",raw), \
                 patch.object(mcp_server,"materialize_session_page",archive), \
                 patch.object(mcp_server,"list_session_archives",listing):
                asked=mcp_server.get_raw_message("m1","s1");self.confirm(permissions)
                self.assertNotIn("approval_token",asked)
                self.assertEqual("owner_confirmation_required",mcp_server.get_raw_message("m2","s1")["status"])
                self.assertEqual("owner_confirmation_required",mcp_server.get_raw_message("m1","s2")["status"])
                mcp_server.get_raw_message("m1","s1")
                self.assertEqual("owner_confirmation_required",mcp_server.get_raw_message("m1","s1")["status"])
                self.assertEqual(1,raw.call_count)

                list_asked=mcp_server.session_archive(limit=9);self.confirm(permissions)
                self.assertEqual("owner_confirmation_required",mcp_server.session_archive("s1",9)["status"])
                listed=mcp_server.session_archive(limit=9)
                self.assertEqual("s1",listed["sessions"][0]["session_id"])
                detail_asked=mcp_server.session_archive("s1");self.confirm(permissions)
                self.assertEqual("owner_confirmation_required",mcp_server.session_archive("s2")["status"])
                detail=mcp_server.session_archive("s1")
                self.assertEqual("s1",detail["session_id"])
                self.assertNotIn("conversation",detail)
                self.assertEqual(1,detail["returned"])

    def test_wrong_identity_and_secret_are_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,registry,permissions,identity,_=self.harness(Path(temporary))
            wrong_secret=registry.authenticate("codex","jiedu-client-v1.fake.invalid")
            wrong_identity=registry.authenticate("other",identity.authenticated_id)
            self.assertEqual(PolicyDecision.DENY,permissions.authorize(wrong_secret,"evidence","read",{"message_id":"m1"}).outcome)
            self.assertEqual(PolicyDecision.DENY,permissions.authorize(wrong_identity,"evidence","read",{"message_id":"m1"}).outcome)

    def test_legacy_search_tools_cannot_return_mixed_index_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,_,permissions,identity,gate=self.harness(Path(temporary))
            formal={"source_path":"02_项目/demo.md","content":"formal"}
            routed=Mock(side_effect=lambda *args,**kwargs:{"results":[formal],"observability":{"returned":1}})
            direct=Mock(side_effect=lambda *args,**kwargs:{"results":[formal],"observability":{"returned":1}})
            self.assertEqual('hybrid',inspect.signature(mcp_server.knowledge_search).parameters['mode'].default)
            with self.mcp_patches(identity,permissions,gate),patch.object(mcp_server,"retrieve",routed),patch.object(mcp_server,"direct_knowledge_search",direct):
                self.assertEqual(["02_项目/demo.md"],[row["source_path"] for row in mcp_server.keyword_search("x")])
                self.assertEqual(["02_项目/demo.md"],[row["source_path"] for row in mcp_server.vector_search("x",source_type='markdown',folder='02_项目',project='demo')])
                self.assertEqual(["02_项目/demo.md"],[row["source_path"] for row in mcp_server.hybrid_search("x",source_type='markdown',folder='02_项目',project='demo')])
                result=mcp_server.search_memory("x")
                self.assertEqual(["02_项目/demo.md"],[row["source_path"] for row in result["results"]])
            self.assertTrue(all(call.kwargs.get("scope")=="formal" for call in routed.call_args_list))
            self.assertEqual(['vector','hybrid','hybrid'],[call.kwargs['mode'] for call in direct.call_args_list])
            for call in direct.call_args_list[:2]:
                self.assertEqual(('markdown','02_项目','demo'),(call.kwargs['source_type'],call.kwargs['folder'],call.kwargs['project']))


class RedactionRegressionTests(unittest.TestCase):
    SENTINEL="jiedu-client-v1.11111111-1111-1111-1111-111111111111.FICTIONAL_SENTINEL_ONLY"

    def test_status_preview_audit_and_errors_never_emit_sentinel_or_approval_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);adapter=client_config.CodexAdapter();adapter.path=root/"config.toml"
            adapter.path.write_text('[mcp_servers.knowledge_bridge]\ncommand="python"\nargs=["-m","knowledge_bridge.cli","mcp"]\n[mcp_servers.knowledge_bridge.env]\nKNOWLEDGE_BRIDGE_CLIENT_ID="codex"\nKNOWLEDGE_BRIDGE_CLIENT_SECRET="'+self.SENTINEL+'"\n',"utf-8")
            with patch.dict(client_config.ADAPTERS,{"codex":adapter},clear=True), \
                 patch.object(client_config,"DATA",root/"state"),patch.object(client_config,"BACKUPS",root/"backups"), \
                 patch.object(client_config,"AUDIT",root/"config-audit.jsonl"),patch.object(client_config,"PENDING",root/"pending.json"):
                status=client_config.statuses();serialized=json.dumps(status)
                self.assertNotIn("entry",status[0]);self.assertNotIn(self.SENTINEL,serialized)
                preview=client_config.preview("codex","install",client_id="codex",secret=self.SENTINEL,ephemeral=True)
                self.assertNotIn(self.SENTINEL,json.dumps(preview));client_config.cancel(preview["token"])

            audit=root/"mcp-audit.jsonl"
            with patch.object(mcp_audit,"AUDIT_PATH",audit):
                try:
                    with mcp_audit.audited("codex","fixture",{"nested":{"approval_token":"approval-fixture","secret":self.SENTINEL}}):
                        raise RuntimeError("Authorization: Bearer "+self.SENTINEL)
                except RuntimeError:
                    pass
            text=audit.read_text("utf-8")
            self.assertNotIn(self.SENTINEL,text);self.assertNotIn("approval-fixture",text)

            unsafe={"entry":{"env":{"KNOWLEDGE_BRIDGE_CLIENT_SECRET":self.SENTINEL}},"last_error":self.SENTINEL}
            self.assertNotIn(self.SENTINEL,json.dumps(local_ai.redact(unsafe)))

    def test_real_local_ai_status_response_has_safe_dto_only(self):
        script=r'''
import json,os
from fastapi.testclient import TestClient
from knowledge_bridge.web import app
with TestClient(app,base_url="http://127.0.0.1:8765") as client:
    response=client.get("/api/local-ai/status")
    text=response.text
    body=response.json()
    formal=client.get("/api/document",params={"path":"00_系统/formal.md"})
    evidence=client.get("/api/document",params={"path":"90_原始档案/evidence.md"})
    raw_search=client.get("/api/search",params={"q":"needle","scope":"evidence"})
    sessions=client.get("/api/session-archives")
    print(json.dumps({"status":response.status_code,"has_entry":any("entry" in row for row in body["clients"]),"has_sentinel":os.environ["FIXTURE_SENTINEL"] in text,"has_auth":"Authorization" in text,"credential_present":next(row for row in body["clients"] if row["id"]=="codex")["credential_present"],"formal_status":formal.status_code,"evidence_status":evidence.status_code,"raw_search_status":raw_search.status_code,"sessions_status":sessions.status_code}))
'''
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);workspace=initialize_workspace(root/"workspace");codex=root/"codex"
            (workspace.root/"00_系统/formal.md").write_text("formal needle","utf-8");(workspace.root/"90_原始档案/evidence.md").write_text("evidence needle","utf-8")
            codex.mkdir();(codex/"config.toml").write_text('[mcp_servers.knowledge_bridge]\ncommand="'+sys.executable.replace('\\','\\\\')+'"\nargs=["-m","knowledge_bridge.cli","mcp"]\n[mcp_servers.knowledge_bridge.env]\nKNOWLEDGE_BRIDGE_CLIENT_ID="codex"\nKNOWLEDGE_BRIDGE_CLIENT_SECRET="'+self.SENTINEL+'"\n',"utf-8")
            env=os.environ.copy();env.update({"KNOWLEDGE_BRIDGE_VAULT":str(workspace.root),"KNOWLEDGE_BRIDGE_LOCAL_DATA":str(root/"machine"),"KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE":"1","CODEX_HOME":str(codex),"FIXTURE_SENTINEL":self.SENTINEL,"PYTHONUTF8":"1"})
            done=subprocess.run([sys.executable,"-c",script],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=90)
            self.assertEqual(0,done.returncode,done.stderr);data=json.loads(done.stdout.strip().splitlines()[-1])
            self.assertEqual(200,data["status"]);self.assertFalse(data["has_entry"]);self.assertFalse(data["has_sentinel"]);self.assertFalse(data["has_auth"]);self.assertTrue(data["credential_present"])
            self.assertEqual(200,data["formal_status"]);self.assertEqual(200,data["evidence_status"]);self.assertEqual(200,data["raw_search_status"]);self.assertEqual(200,data["sessions_status"])


if __name__=="__main__":unittest.main()
