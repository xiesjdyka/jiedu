import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_bridge.client_access as client_access_module
from knowledge_bridge.client_access import (
    ClientIdentity, ClientIdentityRegistry, ClientPermissionService, PolicyDecision,
    ORDINARY_DEFAULT_ALLOW, stdio_identity,
)
from knowledge_bridge.workspace import initialize_workspace


class ClientRegistryTests(unittest.TestCase):
    def test_missing_ordinary_rules_default_allow_without_overwriting_explicit_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/'workspace')
            registry=ClientIdentityRegistry(context,base/'machine')
            created=registry.create('old_agent','Old Agent','test','stdio')
            identity=registry.authenticate('old_agent',created['secret'])
            service=ClientPermissionService(registry)
            self.assertEqual(14,len(ORDINARY_DEFAULT_ALLOW))
            self.assertEqual(14,len(service.default_policies()))
            self.assertEqual([],service.policies(identity.internal_id))
            for scope,operation in ORDINARY_DEFAULT_ALLOW:
                with self.subTest(scope=scope.value,operation=operation.value):
                    self.assertEqual(PolicyDecision.ALLOW,
                        service.authorize(identity,scope.value,operation.value).outcome)
                    self.assertEqual(PolicyDecision.ALLOW,
                        service._policy(identity.internal_id,scope,operation,None))
            self.assertEqual([],service.policies(identity.internal_id))
            self.assertEqual(PolicyDecision.DENY,
                service.authorize(identity,'protected','read_protected').outcome)
            service.set_policy(identity.internal_id,'workspace','read','deny')
            self.assertEqual(PolicyDecision.DENY,
                service.authorize(identity,'workspace','read',{'project':'one'}).outcome)
            service.set_policy(identity.internal_id,'workspace','read','ask',{'project':'one'})
            self.assertEqual(PolicyDecision.ASK,
                service.authorize(identity,'workspace','read',{'project':'one'}).outcome)
            self.assertEqual(PolicyDecision.DENY,
                service.authorize(identity,'workspace','read',{'project':'two'}).outcome)
            self.assertEqual(2,len(service.policies(identity.internal_id)))

    def test_persisted_approval_expiry_uses_cross_process_wall_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            self.assertIs(time.time,ClientPermissionService(registry)._clock)

    def test_secret_is_one_time_verified_rotated_revoked_and_workspace_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("fictional_agent","Fictional Agent","test","stdio")
            secret=created["secret"]
            self.assertTrue(created["secret_once"])
            self.assertNotIn(secret,registry.registry_path.read_text("utf-8"))
            self.assertEqual("legacy_unverified",registry.authenticate("fictional_agent",None).status)
            self.assertEqual("invalid",registry.authenticate("other_agent",secret).status)
            identity=registry.authenticate("fictional_agent",secret)
            self.assertTrue(identity.authenticated)
            rotated=registry.rotate(created["internal_id"])
            self.assertFalse(registry.authenticate("fictional_agent",secret).authenticated)
            self.assertTrue(registry.authenticate("fictional_agent",rotated["secret"]).authenticated)
            verifier_before_revoke=registry._load()["clients"][created["internal_id"]]["secret_verifier"]
            registry.revoke(created["internal_id"])
            self.assertEqual("revoked",registry.authenticate("fictional_agent",rotated["secret"]).status)
            self.assertEqual(verifier_before_revoke,
                             registry._load()["clients"][created["internal_id"]]["secret_verifier"])
            other=initialize_workspace(base/"copy")
            self.assertEqual([],ClientIdentityRegistry(other,base/"machine").list_clients())

    def test_stdio_claimed_identity_cannot_override_verified_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("stdio_agent","Stdio Agent","test","stdio")
            good=stdio_identity(context,base/"machine",{"KNOWLEDGE_BRIDGE_CLIENT_ID":"stdio_agent","KNOWLEDGE_BRIDGE_CLIENT_SECRET":created["secret"]})
            bad=stdio_identity(context,base/"machine",{"KNOWLEDGE_BRIDGE_CLIENT_ID":"other_agent","KNOWLEDGE_BRIDGE_CLIENT_SECRET":created["secret"]})
            self.assertTrue(good.authenticated);self.assertFalse(bad.authenticated)

    def test_deny_ask_allow_binding_expiry_and_replay(self):
        class Clock:
            value=10.0
            def __call__(self):return self.value
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("policy_agent","Policy Agent","test","localhost_http")
            identity=registry.authenticate("policy_agent",created["secret"]);clock=Clock()
            service=ClientPermissionService(registry,clock=clock)
            denied=service.authorize(identity,"protected","read_protected",{"object_token":"one"})
            self.assertEqual(PolicyDecision.DENY,denied.outcome)
            service.set_policy(identity.internal_id,"protected","read_protected","ask")
            asked=service.authorize(identity,"protected","read_protected",{"object_token":"one"},approval_ttl_seconds=2)
            self.assertEqual(PolicyDecision.ASK,asked.outcome)
            approval_id=service.pending()[0]["approval_id"];service.confirm(approval_id)
            swapped=service.authorize(identity,"protected","read_protected",{"object_token":"two"},asked.approval_token)
            self.assertEqual(PolicyDecision.DENY,swapped.outcome)
            allowed=service.authorize(identity,"protected","read_protected",{"object_token":"one"},asked.approval_token)
            self.assertEqual(PolicyDecision.ALLOW,allowed.outcome)
            replay=service.authorize(identity,"protected","read_protected",{"object_token":"one"},asked.approval_token)
            self.assertEqual(PolicyDecision.DENY,replay.outcome)
            asked2=service.authorize(identity,"protected","read_protected",{"object_token":"three"},approval_ttl_seconds=1)
            clock.value+=2
            self.assertFalse(service.pending())
            self.assertEqual(PolicyDecision.DENY,service.authorize(identity,"protected","read_protected",{"object_token":"three"},asked2.approval_token).outcome)
            with self.assertRaises(ValueError):service.set_policy(identity.internal_id,"protected","export_protected","allow")

    def test_repeated_pending_request_refreshes_its_expiry(self):
        class Clock:
            value=100.0
            def __call__(self):return self.value
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("refresh_agent","Refresh Agent","test","stdio")
            identity=registry.authenticate("refresh_agent",created["secret"]);clock=Clock()
            service=ClientPermissionService(registry,clock=clock)
            service.set_policy(identity.internal_id,"evidence","search","ask")
            service.authorize(identity,"evidence","search",{"request_digest":"same"},approval_ttl_seconds=5)
            clock.value=104.0
            service.authorize(identity,"evidence","search",{"request_digest":"same"},approval_ttl_seconds=5)
            clock.value=106.0
            self.assertEqual(1,len(service.pending()))

    def test_owner_confirmation_starts_a_fresh_single_use_window(self):
        class Clock:
            value=100.0
            def __call__(self):return self.value
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("confirm_agent","Confirm Agent","test","stdio")
            identity=registry.authenticate("confirm_agent",created["secret"]);clock=Clock()
            service=ClientPermissionService(registry,clock=clock)
            service.set_policy(identity.internal_id,"evidence","search","ask")
            asked=service.authorize(identity,"evidence","search",{"request_digest":"late"},approval_ttl_seconds=5)
            clock.value=104.9;service.confirm(service.pending()[0]["approval_id"])
            self.assertEqual(1,len(service.confirmed()))
            clock.value=108.0
            allowed=service.authorize(identity,"evidence","search",{"request_digest":"late"},asked.approval_token)
            self.assertEqual(PolicyDecision.ALLOW,allowed.outcome)
            self.assertFalse(service.confirmed())
            self.assertEqual(PolicyDecision.DENY,service.authorize(identity,"evidence","search",{"request_digest":"late"},asked.approval_token).outcome)

    def test_permission_transactions_do_not_lose_parallel_mcp_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("parallel_agent","Parallel Agent","test","stdio")
            identity=registry.authenticate('parallel_agent',created['secret'])
            first=ClientPermissionService(registry);second=ClientPermissionService(registry)
            first.set_policy(identity.internal_id,"evidence","search","ask")
            original_atomic=client_access_module._atomic
            def slow_atomic(path,value):
                time.sleep(.05);original_atomic(path,value)
            barrier=threading.Barrier(3);errors=[]
            def request(service,digest):
                try:
                    barrier.wait();service.authorize(identity,"evidence","search",{"request_digest":digest})
                except Exception as exc:errors.append(exc)
            threads=[threading.Thread(target=request,args=(first,"one")),threading.Thread(target=request,args=(second,"two"))]
            with patch("knowledge_bridge.client_access._atomic",side_effect=slow_atomic):
                for thread in threads:thread.start()
                barrier.wait()
                for thread in threads:thread.join(3)
            self.assertFalse(errors)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(2,len(first.pending()))

    def test_encryption_off_defaults_open_but_respects_explicit_deny(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);context=initialize_workspace(base/"workspace")
            registry=ClientIdentityRegistry(context,base/"machine")
            created=registry.create("open_agent","Open Agent","test","stdio")
            identity=registry.authenticate("open_agent",created["secret"])
            service=ClientPermissionService(registry)
            settings=base/"protection-control.json"
            settings.write_text(json.dumps({"workspaces":{context.workspace_id:{"protected_storage_enabled":False}}}),"utf-8")
            with patch("knowledge_bridge.client_access.local_path",side_effect=lambda _name:settings):
                service.set_policy(identity.internal_id,"formal","search","deny")
                service.set_policy(identity.internal_id,"evidence","read","deny")
                service.set_policy(identity.internal_id,"inbox","read","deny")
                for scope,operation in (("formal","search"),("evidence","read"),("inbox","read")):
                    decision=service.authorize(identity,scope,operation)
                    self.assertEqual(PolicyDecision.DENY,decision.outcome)
                    self.assertEqual("policy_deny",decision.reason)
                self.assertEqual(PolicyDecision.ALLOW,service.authorize(identity,'formal','read').outcome)
                unauthenticated=registry.authenticate("open_agent","wrong-secret")
                self.assertEqual(PolicyDecision.DENY,service.authorize(unauthenticated,"inbox","read").outcome)
                restarted_registry=ClientIdentityRegistry(context,base/"machine")
                restarted=ClientPermissionService(restarted_registry)
                restarted_identity=restarted_registry.authenticate("open_agent",created["secret"])
                self.assertEqual(PolicyDecision.DENY,restarted.authorize(restarted_identity,"inbox","read").outcome)
                service.set_policy(identity.internal_id,'inbox','read','allow')
                self.assertEqual(PolicyDecision.ALLOW,service.authorize(identity,'inbox','read').outcome)
                self.assertEqual(PolicyDecision.ALLOW,service.authorize(identity,"formal","submit_candidate").outcome)
                self.assertEqual(PolicyDecision.DENY,service.authorize(identity,"protected","read_protected").outcome)
                with self.assertRaises(ValueError):
                    service.grant_session_read(identity.internal_id,"session",["inbox"])
                registry.revoke(identity.internal_id)
                self.assertEqual(PolicyDecision.DENY,service.authorize(identity,"formal","search").outcome)


WEB_SCRIPT=r'''
import base64,json,os,time,tomllib,re
from pathlib import Path
from fastapi.testclient import TestClient
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.storage import StorageAuthorization,StorageOperation
root=Path(os.environ["KB_TEST_WORKSPACE"]);initialize_workspace(root)
from knowledge_bridge.web import app
origin="http://127.0.0.1:8765";result={}
with TestClient(app,base_url=origin) as client:
    status=client.get("/api/protection/status").json();csrf=status["csrf_token"]
    initialized=client.post("/api/protection/initialize",json={"passphrase":"fictional-client-gate-pass","confirmation":"fictional-client-gate-pass"},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    csrf=initialized.json()["csrf_token"]
    created=client.post("/api/client-access/clients",json={"claimed_id":"browser_agent","display_name":"Browser Agent","client_type":"codex","transport":"stdio"},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["create_status"]=created.status_code;body=created.json();secret=body["secret"];internal=body["client"]["internal_id"]
    result["secret_once"]=body["secret_once"]
    result["not_registered_before_commit"]=len(client.get("/api/client-access/status").json()["registered"])==0
    committed=client.post(f'/api/client-access/pairings/{body["pairing_id"]}/commit',json={},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["commit_status"]=committed.status_code
    result["claimed_mismatch"]=client.get("/api/client-access/whoami",headers={"Authorization":f"JieduClient other_agent:{secret}"}).status_code
    app.state.protection_control.provider.write_original("fictional/only.txt",b"fictional protected body",StorageAuthorization.allow(StorageOperation.WRITE_ORIGINAL))
    policy=client.post(f"/api/client-access/clients/{internal}/policy",json={"scope":"protected","operation":"read_protected","decision":"ask"},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["policy_status"]=policy.status_code
    auth={"Authorization":f"JieduClient browser_agent:{secret}"}
    result["cookie_only"]=client.post("/api/client-access/protected/read",json={"object_id":"fictional/only.txt"}).status_code
    first=client.post("/api/client-access/protected/read",json={"object_id":"fictional/only.txt"},headers=auth)
    result["first_status"]=first.status_code;result["approval_token_exposed"]="approval_token" in first.text
    pending=client.get("/api/client-access/confirmations").json()["pending"]
    approval_id=pending[0]["approval_id"]
    result["confirm_bad_origin"]=client.post("/api/client-access/confirmations/approve",json={"approval_id":approval_id},headers={"Origin":"http://evil.invalid","X-Jiedu-CSRF":csrf}).status_code
    confirmed=client.post("/api/client-access/confirmations/approve",json={"approval_id":approval_id},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["confirm_status"]=confirmed.status_code
    read=client.post("/api/client-access/protected/read",json={"object_id":"fictional/only.txt"},headers=auth)
    result["read_status"]=read.status_code;result["body"]=base64.b64decode(read.json()["content"]).decode()
    result["replay"]=client.post("/api/client-access/protected/read",json={"object_id":"fictional/only.txt"},headers=auth).status_code
    client.post(f"/api/client-access/clients/{internal}/session-read",json={"scopes":["protected"]},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    time.sleep(3.2)
    expired=client.get("/api/protection/status").json();result["owner_expired"]=not expired["owner_session_active"]
    result["storage_stays_unlocked"]=expired["state"]=="unlocked"
    result["read_after_owner_expiry"]=client.post("/api/client-access/protected/read",json={"object_id":"fictional/only.txt"},headers=auth).status_code
    result["admin_after_owner_expiry"]=client.post(f"/api/client-access/clients/{internal}/policy",json={"scope":"formal","operation":"search","decision":"allow"},headers={"Origin":origin,"X-Jiedu-CSRF":expired["csrf_token"]}).status_code
    renewed=client.post("/api/protection/unlock/password",json={"passphrase":"fictional-client-gate-pass"},headers={"Origin":origin,"X-Jiedu-CSRF":expired["csrf_token"]})
    result["owner_reauthenticated"]=renewed.status_code;csrf=renewed.json()["csrf_token"]
    rotation=client.post(f"/api/client-access/clients/{internal}/rotate",json={},headers={"Origin":origin,"X-Jiedu-CSRF":csrf}).json()
    result["rotation_preview_has_secret"]=bool(re.search(r"jiedu-client-v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",json.dumps(rotation)))
    result["old_before_rotation_commit"]=client.get("/api/client-access/whoami",headers=auth).status_code
    committed_rotation=client.post(f'/api/client-access/rotations/{rotation["rotation_id"]}/commit',json={},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["rotation_commit_status"]=committed_rotation.status_code
    config=tomllib.loads((Path(os.environ["CODEX_HOME"])/"config.toml").read_text("utf-8"))
    new_secret=config["mcp_servers"]["knowledge_bridge"]["env"]["KNOWLEDGE_BRIDGE_CLIENT_SECRET"]
    result["old_after_rotate"]=client.get("/api/client-access/whoami",headers=auth).status_code
    new_auth={"Authorization":f"JieduClient browser_agent:{new_secret}"}
    result["new_after_rotate"]=client.get("/api/client-access/whoami",headers=new_auth).status_code
    client.post(f"/api/client-access/clients/{internal}/revoke",json={},headers={"Origin":origin,"X-Jiedu-CSRF":csrf})
    result["after_revoke"]=client.get("/api/client-access/whoami",headers=new_auth).status_code
    public=client.get("/api/client-access/status").json();result["status_has_secret"]="secret" in json.dumps(public).lower()
    audit=client.get("/api/client-access/audit").json();result["audit_has_secret"]=secret in json.dumps(audit)
    machine=Path(os.environ["KNOWLEDGE_BRIDGE_LOCAL_DATA"])/"client-identities"
    disk=b"".join(path.read_bytes() for path in machine.rglob("*") if path.is_file())
    result["secret_on_disk"]=secret.encode() in disk or new_secret.encode() in disk
print(json.dumps(result,ensure_ascii=False))
'''


class ClientAccessWebTests(unittest.TestCase):
    def test_http_auth_owner_confirmation_rotation_and_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);project=Path(__file__).resolve().parents[1]
            runtime=project/".venv312"/"Scripts"/"python.exe"
            env=os.environ.copy();env.update({"KB_TEST_WORKSPACE":str(base/"workspace"),
                "KNOWLEDGE_BRIDGE_VAULT":str(base/"workspace"),"KNOWLEDGE_BRIDGE_LOCAL_DATA":str(base/"machine"),
                "KNOWLEDGE_BRIDGE_PROTECTION_TEST_MODE":"1","PYTHONUTF8":"1","CODEX_HOME":str(base/"codex-home")})
            done=subprocess.run([str(runtime if runtime.exists() else Path(sys.executable)),"-c",WEB_SCRIPT],cwd=project,env=env,
                capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=90)
            self.assertEqual(0,done.returncode,done.stderr)
            data=json.loads(done.stdout.strip().splitlines()[-1])
            self.assertEqual(200,data["create_status"]);self.assertTrue(data["secret_once"])
            self.assertTrue(data["not_registered_before_commit"]);self.assertEqual(200,data["commit_status"])
            self.assertEqual(401,data["claimed_mismatch"])
            self.assertEqual(200,data["policy_status"]);self.assertEqual(401,data["cookie_only"])
            self.assertEqual(428,data["first_status"]);self.assertFalse(data["approval_token_exposed"]);self.assertEqual(403,data["confirm_bad_origin"])
            self.assertEqual(200,data["confirm_status"]);self.assertEqual(200,data["read_status"])
            self.assertEqual("fictional protected body",data["body"]);self.assertEqual(428,data["replay"])
            self.assertTrue(data["owner_expired"]);self.assertTrue(data["storage_stays_unlocked"])
            self.assertEqual(200,data["read_after_owner_expiry"]);self.assertEqual(403,data["admin_after_owner_expiry"])
            self.assertEqual(200,data["owner_reauthenticated"])
            self.assertFalse(data["rotation_preview_has_secret"]);self.assertEqual(200,data["old_before_rotation_commit"])
            self.assertEqual(200,data["rotation_commit_status"])
            self.assertEqual(401,data["old_after_rotate"]);self.assertEqual(200,data["new_after_rotate"])
            self.assertEqual(401,data["after_revoke"]);self.assertFalse(data["status_has_secret"]);self.assertFalse(data["audit_has_secret"]);self.assertFalse(data["secret_on_disk"])


if __name__=="__main__":unittest.main()
