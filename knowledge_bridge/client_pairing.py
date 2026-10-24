"""Owner-confirmed pairing transaction across client config, identity and policy."""
from __future__ import annotations

import secrets
import hashlib
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
from .client_visibility import hidden_client_types


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
        self.ttl_seconds=max(60,int(ttl_seconds));self._pending={};self._handoffs={};self._lock=threading.RLock()

    def _expire(self) -> None:
        now=time.monotonic()
        for pairing_id,row in list(self._pending.items()):
            if row["expires_at"]<=now:
                if row.get('config_token'):config_cancel(row['config_token'])
                self._pending.pop(pairing_id,None)
        self._handoffs={key:row for key,row in self._handoffs.items() if row['expires_at']>now}

    def issue_handoff(self, client_type: str) -> dict:
        """Owner-issued, short-lived capability for one local branded pairing."""
        if client_type not in {'codex','hermes','dsh','claude','cursor'}:raise ValueError('该客户端尚无已验证的接入流程')
        with self._lock:
            self._expire()
            token=secrets.token_urlsafe(32)
            self._handoffs[hashlib.sha256(token.encode()).hexdigest()]={
                'client_type':client_type,'workspace_id':self.registry.context.workspace_id,
                'expires_at':time.monotonic()+300}
            return {'token':token,'expires_in_seconds':300,'client_type':client_type,
                    'workspace_id':self.registry.context.workspace_id}

    def redeem_handoff(self, token: str, client_type: str, workspace_id: str) -> dict:
        if not isinstance(token,str) or len(token)>128:raise ValueError('接入授权无效或已过期')
        with self._lock:
            self._expire()
            key=hashlib.sha256(token.encode()).hexdigest()
            row=self._handoffs.get(key)
            if not row or row['client_type']!=client_type or row['workspace_id']!=workspace_id:
                raise ValueError('接入授权无效或已过期')
            self._handoffs.pop(key,None)
            return self.quick_connect(client_type)

    @staticmethod
    def _adapter(client_type: str, transport: str) -> str:
        adapter=client_type.strip().casefold()
        if transport!='stdio':raise ValueError('通用 HTTP 配对尚未就绪：现有 /mcp 分支缺少逐客户端 Headers 认证与独立服务接线，请选择 STDIO')
        if adapter in {'custom','other','local_agent','workbuddy','opencode','zcode','kimi'}:return 'manual'
        if adapter == 'doubao_work':return 'doubao_work'
        if adapter not in {'codex','hermes','dsh','claude','cursor'}:raise ValueError('未知客户端类型')
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
        rows = [
            {"id":"codex","label":"Codex","display_name":"我的 Codex","suggested_claimed_id":unique("codex"),"configuration":"verified_template","transport":"stdio","template_note":"连接后由界渡后台补齐可读取的本机 JSONL 历史并持续采集；MCP 配置应用后需重启 Codex。"},
            {"id":"hermes","label":"Hermes Agent","display_name":"我的 Hermes","suggested_claimed_id":unique("hermes"),"configuration":"verified_template","transport":"stdio","template_note":"连接后由界渡后台补齐可访问历史并持续采集新消息。"},
            {"id":"dsh","label":"DSH","display_name":"我的 DSH","suggested_claimed_id":unique("dsh"),"configuration":"verified_patch","transport":"stdio","template_note":"通过 DSH 桌面 profile 补丁接入；已有配置条目保持不变。"},
            {"id":"cursor","label":"Cursor","display_name":"我的 Cursor","suggested_claimed_id":unique("cursor"),"configuration":"verified_template","transport":"stdio","template_note":"由你在 Cursor 接入 MCP 并完成真实读取后，首次连接自动采集本机可读历史及新消息；已有暂停与范围保留。"},
            {"id":"workbuddy","label":"WorkBuddy","display_name":"我的 WorkBuddy","suggested_claimed_id":unique("workbuddy"),"configuration":"manual_fields","transport":"stdio","template_note":"交付独立 STDIO 字段，由你在 WorkBuddy 完成接入；成功读取后才启用本机历史采集。"},
            {"id":"opencode","label":"OpenCode","display_name":"我的 OpenCode","suggested_claimed_id":unique("opencode"),"configuration":"manual_fields","transport":"stdio","template_note":"交付独立 STDIO 字段，由你在 OpenCode 完成接入；成功读取后才启用本机历史采集。"},
            {"id":"zcode","label":"ZCode","display_name":"我的 ZCode","suggested_claimed_id":unique("zcode"),"configuration":"manual_fields","transport":"stdio","template_note":"交付独立 STDIO 字段，由你在 ZCode 完成接入；成功读取后才启用本机历史采集。"},
            {"id":"kimi","label":"Kimi Work","display_name":"我的 Kimi Work","suggested_claimed_id":unique("kimi"),"configuration":"manual_fields","transport":"stdio","template_note":"交付独立 STDIO 字段，由你在 Kimi Work 完成接入；成功读取后才启用本机历史采集。"},
            {"id":"claude","label":"Claude","display_name":"我的 Claude","suggested_claimed_id":unique("claude"),"configuration":"desktop_extension","transport":"stdio","template_note":"准备 Claude Desktop 扩展包，由你在客户端安装；成功读取后才算连接。"},
            {"id":"doubao_work","label":"豆包工作版","display_name":"我的豆包工作版","suggested_claimed_id":unique("doubao_work"),"configuration":"local_source_auto_detect","transport":"stdio","template_note":"自动发现豆包已落盘的工作会话并持续归档；不会修改豆包文件。"},
            {"id":"custom","label":"自定义","display_name":"我的 AI","suggested_claimed_id":unique("custom_ai"),"configuration":"manual_fields","transport":"stdio","template_note":"交付完整 STDIO 命令、独立参数和环境变量；不会猜测客户端格式。"},
        ]
        hidden = hidden_client_types(self.registry.context)
        return [row for row in rows if row["id"] not in hidden]

    def quick_connect(self, client_type: str, profile_id: str | None = None) -> dict:
        """Owner-authorized branded pairing; reuse every existing local connection."""
        if client_type in hidden_client_types(self.registry.context):
            raise ValueError('该客户端已从当前工作区隐藏；请先恢复入口')
        if client_type in {'workbuddy','opencode','zcode','kimi'}:
            return self._quick_connect_manual(client_type)
        if client_type not in {'codex', 'hermes', 'dsh', 'claude', 'cursor'}:
            raise ValueError('该客户端尚无已验证的快捷接入流程')
        if client_type=='claude':
            return self._quick_connect_claude_extension(profile_id)
        from .client_config import ADAPTERS, _entry_identity, _valid_entry
        with self._lock:
            matches = [row for row in self.registry.list_clients()
                       if row.get('client_type') == client_type and row.get('status') != 'revoked']
            detected = ADAPTERS[client_type].detect()
            entry=detected.get('entry')
            env=entry.get('env') if isinstance(entry,dict) else None
            secret=env.get('KNOWLEDGE_BRIDGE_CLIENT_SECRET') if isinstance(env,dict) else None
            configured_id=_entry_identity(entry)
            current = next((row for row in matches if configured_id==row['claimed_id'] and
                            _valid_entry(entry,row['claimed_id']) and
                            self.registry.configured_secret_matches(row['internal_id'],secret)), None)
            if current:
                return {'outcome': 'reused', 'client': current, 'restart_required': False,
                        'auto_archive':self._ensure_existing_collection(client_type)}
            if len(matches)>1:
                raise ValueError('该客户端有多个现存身份，当前配置未能匹配；请先选择并核对，界渡不会轮换其中任何一个')
            current=matches[0] if matches else None
            if current:
                if entry and (configured_id!=current['claimed_id'] or
                              entry.get('unmanaged_existing') or entry.get('invalid_existing')):
                    raise ValueError('当前 MCP 配置不属于这条身份；原配置和权限保留，请先核对')
                if not detected.get('installed'):
                    raise ValueError('本机客户端未检测到，不能修复 MCP 配置')
                rotation=self.begin_rotation(current['internal_id'])
                repaired=self.commit_rotation(rotation['rotation_id'])
                return {'outcome':'repaired','client':repaired['client'],
                        'restart_required':repaired['restart_required'],
                        'auto_archive':self._ensure_existing_collection(client_type)}
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

    def _quick_connect_manual(self, client_type: str) -> dict:
        """One-time STDIO handoff; the client owns its unknown config format."""
        with self._lock:
            matches=[row for row in self.registry.list_clients()
                     if row.get('client_type')==client_type and row.get('status')!='revoked']
            if matches:
                if len(matches)>1:
                    raise ValueError('该来源已有多个身份；请先选择并核对，界渡不会新建或轮换')
                return {'outcome':'reused','client':matches[0],
                        'manual_configuration':True,'secret_available':False,
                        'auto_archive':'existing_settings_preserved'}
            preset=next(row for row in self.presets() if row['id']==client_type)
            pairing=self.begin(preset['suggested_claimed_id'],preset['display_name'],client_type,'stdio')
            try:
                result=self.commit(pairing['pairing_id'])
            except Exception:
                self.cancel(pairing['pairing_id'])
                raise
            return {'outcome':'paired','client':result['client'],
                    'manual_configuration':True,'delivery':result['delivery'],
                    'secret':pairing['secret'],'secret_once':True,
                    'auto_archive':'waiting_for_verified_read'}

    def _quick_connect_claude_extension(self, profile_id: str | None = None) -> dict:
        """Prepare an MCPB; only Claude's own install and a real read finish connection."""
        from .claude_extension import BUNDLE, inspect_bundle, prepare_bundle
        from .claude_source_adapter import profile_paths
        with self._lock:
            profiles=[key for key, (_, database, _) in profile_paths().items()
                      if database.parent.parent.is_dir()]
            if not profiles:
                raise ValueError('本机尚未发现 Claude Desktop 资料目录')
            if profile_id is not None and profile_id not in profiles:
                raise ValueError('选择的 Claude 资料目录不可用')
            if len(profiles)>1 and not profile_id:
                raise ValueError('检测到多个 Claude 资料目录；请先明确选择目标，界渡不会猜测')
            chosen=profile_id or profiles[0]
            matches=[row for row in self.registry.list_clients()
                     if row.get('client_type')=='claude' and row.get('status')!='revoked']
            bundle=inspect_bundle()
            if bundle:
                matched=next((row for row in matches if row['claimed_id']==bundle['client_id'] and
                              self.registry.configured_secret_matches(row['internal_id'],bundle['secret'])),None)
                if matched:
                    return {'outcome':'reused','client':matched,'restart_required':False,
                            'auto_archive':self._ensure_existing_collection('claude',chosen),
                            'extension_ready':True,'extension_download':'/api/client-access/claude-extension/download'}
                raise ValueError('已存在待安装的 Claude 扩展包，当前身份无法确认；原包保留等待核对')
            if matches:
                raise ValueError('已有 Claude 身份但扩展包缺失；请先核对原连接，界渡不会重建身份或权限')
            preset=next(row for row in self.presets() if row['id']=='claude')
            prepared=self.registry.prepare(preset['suggested_claimed_id'],preset['display_name'],'claude','stdio')
            prepared['archive_source']=SourceConnections.descriptor('claude')
            created=False;committed=None
            try:
                prepare_bundle(prepared['claimed_id'],prepared['secret'])
                created=True
                committed=self.registry.commit_prepared(prepared)
                for scope,operation,decision in self._policies(None):
                    self.permissions.set_policy(committed['internal_id'],scope,operation,decision)
                SourceConnections(self.registry.context).bind(committed['internal_id'],prepared['archive_source'])
                self.registry.audit('claude_extension_prepared',committed['claimed_id'],
                                    committed['internal_id'],'success',adapter='claude')
            except Exception:
                if committed:
                    try:self.permissions.remove_client(committed['internal_id'])
                    finally:self.registry.rollback_new(committed['internal_id'])
                if created:BUNDLE.unlink(missing_ok=True)
                raise
            collection=self._ensure_existing_collection('claude',chosen)
            return {'outcome':'paired','client':committed,'restart_required':True,
                    'auto_archive':collection,'extension_ready':True,
                    'extension_download':'/api/client-access/claude-extension/download'}

    def _ensure_existing_collection(self, client_type: str, profile_id: str | None = None) -> str:
        """Only fill an absent collector state; preserve pause, scope and cursors."""
        try:
            if client_type=='cursor':
                return 'waiting_for_verified_read'
            if client_type=='codex':
                from .codex_source_adapter import CodexSourceAdapter
                return 'enabled' if CodexSourceAdapter(self.registry.context).enable_for_first_pairing() else 'existing_settings_preserved'
            if client_type=='hermes':
                from .hermes_session_adapter import enable_history_for_first_pairing
                return enable_history_for_first_pairing()
            if client_type=='dsh':
                from .dsh_source_adapter import DSHSourceAdapter
                adapter=DSHSourceAdapter(self.registry.context)
            else:
                from .claude_source_adapter import ClaudeSourceAdapter
                adapter=ClaudeSourceAdapter(self.registry.context,profile_id=profile_id)
            if adapter.state_path.exists():return 'existing_settings_preserved'
            if client_type=='claude' and (adapter.leveldb is None or not (adapter.leveldb/'CURRENT').is_file()):
                return 'waiting_for_source'
            adapter.configure('all',owner_confirmed=True)
            return 'enabled' if adapter.status()['source_detected'] else 'waiting_for_source'
        except Exception:
            return 'activation_failed'

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
        if client_type in hidden_client_types(self.registry.context):
            raise ValueError('该客户端已从当前工作区隐藏；请先恢复入口')
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
                    'raw_archive_note':('豆包本机轨迹属于局部来源：用户消息与无工具调用的助手答复会自动归档；工具过程被过滤，公开助手尾部可能延迟或缺失。' if adapter=='doubao_work' else '以上选择只作用于当前客户端。未单独设置的普通权限默认允许；已有明确拒绝或每次询问优先。受保护知识仍需单独授予本次会话；提交候选不等于正式写入。')}
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
                    "restart_required":adapter in {"codex","claude"},
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
                        "restart_required":row["adapter"] in {"codex","claude"}}
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
            if row['prepared']['client_type'] in hidden_client_types(self.registry.context):
                if row.get('config_token'):config_cancel(row['config_token'])
                raise ValueError('该客户端已从当前工作区隐藏；配对预览已取消')
            prior_local_source = row['adapter'] in {'codex','hermes','dsh','claude'} and any(
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
                        "restart_required":row["adapter"] in {"codex","claude"},
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
            elif row['adapter'] in {'dsh','claude'}:
                result['auto_archive'] = 'existing_settings_preserved'
                if not prior_local_source:
                    result['auto_archive'] = self._ensure_existing_collection(row['adapter'])
                    if result['auto_archive'] == 'activation_failed':
                        result['collection_warning'] = '连接配置已准备；本机采集仍需检查来源，请在 AI 连接中查看。'
            return result

    def pending_count(self) -> int:
        with self._lock:self._expire();return len(self._pending)
