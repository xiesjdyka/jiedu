"""Owner-confirmed pairing transaction across client config, identity and policy."""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timezone

from .client_access import ClientIdentityRegistry, ClientPermissionService
from .client_config import apply as config_apply
from .client_config import cancel as config_cancel
from .client_config import preview as config_preview
from .client_config import rollback as config_rollback
from .client_config import generic_stdio_config
from .workspace import local_path
from .source_connections import SourceConnections
from .source_adapters import for_client as source_adapter_for_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


POLICY_DEFAULTS = {
    "formal": "allow", "evidence": "allow", "inbox": "allow", "workspace": "allow",
    "formal_write": "allow", "archive_write": "allow", "inbox_write": "allow",
}
FIXED_SAFE_POLICIES = (
    ("protected", "search", "deny"),
    ("protected", "read_protected", "deny"),
    ("protected", "export_protected", "deny"),
    ("protected", "delete", "deny"),
    ("protected", "migrate", "deny"),
)
# Legacy fixture defaults only; new connections use POLICY_DEFAULTS above.
CODEX_INITIAL_POLICIES = (
    ('formal','search','ask'),('formal','read','ask'),('evidence','search','ask'),
    ('evidence','read','ask'),('inbox','search','deny'),('inbox','read','deny'),
    ('formal','context','ask'),('formal','submit_candidate','deny'),
    ('evidence','raw_archive','deny'),*FIXED_SAFE_POLICIES,
)


class PairingTransactionManager:
    """Keeps secrets and uncommitted identities only in process memory."""

    def __init__(self, registry: ClientIdentityRegistry,
                 permissions: ClientPermissionService, ttl_seconds: int = 600):
        self.registry=registry;self.permissions=permissions
        self.ttl_seconds=max(60,int(ttl_seconds));self._pending={};self._lock=threading.RLock()

    def _expire(self) -> None:
        now=time.monotonic()
        for pairing_id,row in list(self._pending.items()):
            if row["expires_at"]<=now:
                if row.get('config_token'):config_cancel(row['config_token'])
                self._pending.pop(pairing_id,None)

    @staticmethod
    def _adapter(client_type: str, transport: str) -> str:
        adapter=client_type.strip().casefold()
        if transport!='stdio':raise ValueError('通用 HTTP 配对尚未就绪：现有 /mcp 分支缺少逐客户端 Headers 认证与独立服务接线，请选择 STDIO')
        if adapter in {'custom','other','local_agent'}:return 'manual'
        if adapter == 'doubao_work':return 'doubao_work'
        if adapter not in {'codex','hermes'}:raise ValueError('未知客户端类型')
        return adapter

    def presets(self) -> list[dict]:
        """Verified configuration choices and collision-free suggested identities."""
        active={row["claimed_id"] for row in self.registry.list_clients() if row["status"]!="revoked"}
        active.update(row["prepared"]["claimed_id"] for row in self._pending.values())
        def unique(base):
            if base not in active:return base
            number=2
            while f"{base}_{number}" in active:number+=1
            return f"{base}_{number}"
        return [
            {"id":"codex","label":"Codex","display_name":"我的 Codex","suggested_claimed_id":unique("codex"),"configuration":"verified_template","transport":"stdio","template_note":"连接后由界渡后台补齐可读取的本机 JSONL 历史并持续采集；MCP 配置应用后需重启 Codex。"},
            {"id":"hermes","label":"Hermes Agent","display_name":"我的 Hermes","suggested_claimed_id":unique("hermes"),"configuration":"verified_template","transport":"stdio","template_note":"连接后由界渡后台补齐可访问历史并持续采集新消息。"},
            {"id":"doubao_work","label":"豆包工作版","display_name":"我的豆包工作版","suggested_claimed_id":unique("doubao_work"),"configuration":"local_source_auto_detect","transport":"stdio","template_note":"自动发现豆包已落盘的工作会话并持续归档；不会修改豆包文件。"},
            {"id":"custom","label":"自定义","display_name":"我的 AI","suggested_claimed_id":unique("custom_ai"),"configuration":"manual_fields","transport":"stdio","template_note":"交付完整 STDIO 命令、独立参数和环境变量；不会猜测客户端格式。"},
        ]

    def quick_connect(self, client_type: str) -> dict:
        """Owner-authorized branded pairing; reuse every existing local connection."""
        if client_type not in {'codex', 'hermes'}:
            raise ValueError('快捷接入只支持 Codex 和 Hermes')
        from .client_config import ADAPTERS
        with self._lock:
            current = next((row for row in self.registry.list_clients()
                            if row.get('client_type') == client_type and row.get('status') != 'revoked'), None)
            if current:
                return {'outcome': 'reused', 'client': current, 'restart_required': False}
            detected = ADAPTERS[client_type].detect()
            if detected.get('entry'):
                return {'outcome': 'existing_configuration', 'restart_required': False}
            if not detected.get('installed'):
                raise ValueError('本机尚未检测到该客户端，请先安装并启动后再接入')
            preset = next(row for row in self.presets() if row['id'] == client_type)
            pairing = self.begin(preset['suggested_claimed_id'], preset['display_name'],
                                 client_type, 'stdio')
            # The existing transaction writes a backup and rolls back both config
            # and identity on failure. Never return the one-time secret to the UI.
            result = self.commit(pairing['pairing_id'])
            return {'outcome': 'paired', 'client': result['client'],
                    'restart_required': result['restart_required'],
                    'auto_archive': result.get('auto_archive'),
                    'collection_warning': result.get('collection_warning')}

    @staticmethod
    def _policies(value:dict|None) -> list[tuple[str,str,str]]:
        choices=POLICY_DEFAULTS|dict(value or {})
        if set(choices)!=set(POLICY_DEFAULTS) or any(v not in {'allow','ask','deny'} for v in choices.values()):
            raise ValueError('权限范围或允许方式无效')
        policies=[(scope,op,choices[scope]) for scope in ('formal','evidence','inbox','workspace') for op in ('search','read')]
        policies.extend([('formal','context',choices['formal']),('inbox','context',choices['inbox']),
                         ('workspace','context',choices['workspace']),
                         ('formal','submit_candidate',choices['formal_write']),
                         ('evidence','raw_archive',choices['archive_write']),
                         ('inbox','save_inbox',choices['inbox_write'])])
        policies.extend(FIXED_SAFE_POLICIES)
        return policies

    def begin(self, claimed_id: str, display_name: str, client_type: str,
              transport: str, read_policies:dict|None=None, product_family:str|None=None) -> dict:
        with self._lock:
            self._expire();adapter=self._adapter(client_type,transport)
            prepared=self.registry.prepare(claimed_id,display_name,client_type,transport)
            prepared['archive_source']=SourceConnections.descriptor(client_type,product_family)
            adapter_plan=source_adapter_for_client(client_type)
            policies=self._policies(read_policies)
            if adapter in {'manual','doubao_work'}:
                delivery=generic_stdio_config(prepared['claimed_id'],self.registry.context.root,local_path())
                pairing_id=secrets.token_urlsafe(32)
                self._pending[pairing_id]={'pairing_id':pairing_id,'prepared':prepared,'adapter':adapter,'config_token':None,'policies':policies,'delivery':delivery,'expires_at':time.monotonic()+self.ttl_seconds}
                return {'pairing_id':pairing_id,'expires_in_seconds':self.ttl_seconds,'secret':prepared['secret'],'secret_once':True,
                    'client':{key:prepared[key] for key in ('internal_id','claimed_id','display_name','client_type','transport')},
                    'archive_source':prepared['archive_source'],'source_adapter':adapter_plan,'manual_configuration':adapter=='manual','delivery':delivery,'config':{'target_path':'豆包本机来源自动发现' if adapter=='doubao_work' else '手工填写客户端 STDIO 表单','diff':'确认后登记豆包工作版来源；界渡只读扫描已落盘会话，不修改豆包配置。' if adapter=='doubao_work' else '确认后交付命令、参数列表和环境变量；不会修改其他软件配置。','restart_required':False},
                    'initial_policies':[{'scope':s,'operation':o,'decision':d} for s,o,d in policies],
                    'raw_archive_note':('豆包本机轨迹属于局部来源：用户消息与无工具调用的助手答复会自动归档；工具过程被过滤，公开助手尾部可能延迟或缺失。' if adapter=='doubao_work' else '以上选择只作用于当前客户端。加密关闭时，已认证客户端的正式知识、原始证据和收件箱普通读取固定开放；受保护知识仍需单独授予本次会话。提交候选不等于正式写入。')}
            preview=config_preview(adapter,"install",client_id=prepared["claimed_id"],
                                   secret=prepared["secret"],ephemeral=True)
            pairing_id=secrets.token_urlsafe(32)
            self._pending[pairing_id]={"pairing_id":pairing_id,"prepared":prepared,
                "adapter":adapter,"config_token":preview["token"],"created_at":_now(),
                "expires_at":time.monotonic()+self.ttl_seconds,"policies":policies}
            return {"pairing_id":pairing_id,"expires_in_seconds":self.ttl_seconds,
                    "secret":prepared["secret"],"secret_once":True,
                    "client":{key:prepared[key] for key in ("internal_id","claimed_id","display_name","client_type","transport")},
                    "archive_source":prepared["archive_source"],"source_adapter":adapter_plan,"config":{key:preview[key] for key in ("target_path","diff","changed","backup_directory","rollback","restart_required")},
                    "initial_policies":[{"scope":scope,"operation":operation,"decision":decision}
                                        for scope,operation,decision in policies],
                    "raw_archive_note":"品牌只决定配置模板，不决定权限；普通权限可逐项关闭，受保护知识需单独授予本次会话。首次确认后界渡后台会补齐可访问历史并持续采集新消息；已有暂停或范围设置保持不变。提交候选仍需 Owner 审核。"}

    def begin_rotation(self, internal_id: str) -> dict:
        with self._lock:
            self._expire();prepared=self.registry.prepare_rotation(internal_id)
            adapter=self._adapter(prepared["client_type"],prepared["transport"])
            if adapter in {'manual','doubao_work'}:
                delivery=generic_stdio_config(prepared['claimed_id'],self.registry.context.root,local_path());key=secrets.token_urlsafe(32)
                self._pending[key]={'kind':'rotation','prepared':prepared,'adapter':adapter,'config_token':None,'delivery':delivery,'expires_at':time.monotonic()+self.ttl_seconds}
                return {'rotation_id':key,'client':{k:prepared[k] for k in ('internal_id','claimed_id','display_name','client_type','transport')},'manual_configuration':True,'delivery':delivery,'config':{'target_path':'客户端环境变量','diff':'确认后旧秘密失效，请用新秘密更新原客户端。'},'reason':'重新交付独立凭据，旧凭据在确认后失效。','task_impact':'请更新客户端后重新连接，不会修改读取策略。'}
            preview=config_preview(adapter,"install",client_id=prepared["claimed_id"],
                                   secret=prepared["secret"],ephemeral=True)
            rotation_id=secrets.token_urlsafe(32)
            self._pending[rotation_id]={"kind":"rotation","pairing_id":rotation_id,
                "prepared":prepared,"adapter":adapter,"config_token":preview["token"],
                "created_at":_now(),"expires_at":time.monotonic()+self.ttl_seconds}
            return {"rotation_id":rotation_id,"expires_in_seconds":self.ttl_seconds,
                    "client":{key:prepared[key] for key in ("internal_id","claimed_id","display_name","client_type","transport")},
                    "reason":"当前配对凭据已被视为暴露，轮换后旧凭据将无法建立新连接。",
                    "config":{key:preview[key] for key in ("target_path","diff","changed","backup_directory","rollback","restart_required")},
                    "restart_required":adapter=="codex",
                    "task_impact":"配置安全写入后当前任务可继续，但 Codex 重启前不会加载新凭据。"}

    def commit_rotation(self, rotation_id: str) -> dict:
        with self._lock:
            self._expire();row=self._pending.pop(rotation_id,None)
            if not row or row.get("kind")!="rotation":raise ValueError("轮换预览已取消或过期")
            config_result=None
            try:
                if row['adapter'] in {'manual','doubao_work'}:
                    client=self.registry.commit_rotation(row['prepared'])
                    return {'rotated':True,'client':client,'manual_configuration':True,'delivery':row['delivery'],'secret':row['prepared']['secret'],'secret_once':True,'restart_required':False}
                config_result=config_apply(row["config_token"])
                client=self.registry.commit_rotation(row["prepared"])
                return {"rotated":True,"client":client,
                        "config":{"target_path":config_result["path"],"backup":config_result["backup"],
                                  "before_sha256":config_result["before_sha256"],"after_sha256":config_result["after_sha256"]},
                        "restart_required":row["adapter"]=="codex"}
            except Exception:
                if config_result:config_rollback(config_result)
                elif row.get('config_token'):config_cancel(row["config_token"])
                raise

    def cancel(self, pairing_id: str) -> dict:
        with self._lock:
            row=self._pending.pop(pairing_id,None)
            if row and row.get('config_token'):config_cancel(row["config_token"])
            return {"cancelled":bool(row),"formal_identity_created":False}

    def commit(self, pairing_id: str) -> dict:
        with self._lock:
            self._expire();row=self._pending.pop(pairing_id,None)
            if not row or row.get("kind")=="rotation":raise ValueError("配对预览已取消或过期，请重新开始")
            prior_local_source = row['adapter'] in {'codex','hermes'} and any(
                client.get('client_type') == row['adapter'] for client in self.registry.list_clients())
            config_result=None;committed=None
            try:
                if row.get('config_token'):config_result=config_apply(row["config_token"])
                committed=self.registry.commit_prepared(row["prepared"])
                policies=row.get('policies',())
                for scope,operation,decision in policies:
                    self.permissions.set_policy(committed["internal_id"],scope,operation,decision)
                if row['adapter'] not in {'manual','doubao_work'}:committed=self.registry.acknowledge_configuration(committed['internal_id'])
                self.registry.audit("pairing_transaction",committed["claimed_id"],
                                    committed["internal_id"],"success",adapter=row["adapter"])
                SourceConnections(self.registry.context).bind(committed['internal_id'],row['prepared']['archive_source'])
                if row['adapter'] in {'manual','doubao_work'}:
                    result={'paired':True,'client':committed,'manual_configuration':row['adapter']=='manual',
                            'delivery':row['delivery'],'restart_required':False,
                            'local_source_collection':row['adapter']=='doubao_work'}
                else:
                    result={"paired":True,"client":committed,
                        "config":{"target_path":config_result["path"],
                                  "backup":config_result["backup"],
                                  "before_sha256":config_result["before_sha256"],
                                  "after_sha256":config_result["after_sha256"]},
                        "restart_required":row["adapter"]=="codex",
                        "restart_note":"Codex 需重启后才会加载新的 MCP 配对配置。" if row["adapter"]=="codex" else None}
            except Exception:
                internal_id=(committed or row['prepared'])['internal_id']
                try:self.permissions.remove_client(internal_id)
                finally:self.registry.rollback_new(internal_id)
                if config_result:config_rollback(config_result)
                elif row.get('config_token'):config_cancel(row["config_token"])
                raise
            # The pairing is now committed. Never start scanning on preview,
            # cancellation, failed commit, credential rotation or an existing source.
            if row['adapter'] == 'codex':
                result['auto_archive'] = 'existing_settings_preserved'
                if not prior_local_source:
                    try:
                        from .codex_source_adapter import CodexSourceAdapter
                        if CodexSourceAdapter(self.registry.context).enable_for_first_pairing():
                            result['auto_archive'] = 'enabled'
                    except Exception as exc:
                        result['auto_archive'] = 'activation_failed'
                        result['collection_warning'] = f'连接已配对；后台归档启用失败（{type(exc).__name__}），请在对话档案中手动启用。'
            elif row['adapter'] == 'hermes':
                result['auto_archive'] = 'existing_settings_preserved'
                if not prior_local_source:
                    try:
                        from .hermes_session_adapter import enable_history_for_first_pairing
                        result['auto_archive'] = enable_history_for_first_pairing()
                    except Exception as exc:
                        result['auto_archive'] = 'activation_failed'
                        result['collection_warning'] = f'连接已配对；历史补齐启用失败（{type(exc).__name__}），请在对话档案中手动启用。'
            return result

    def pending_count(self) -> int:
        with self._lock:self._expire();return len(self._pending)
