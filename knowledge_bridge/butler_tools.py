"""Concrete product operations, deterministic authorization, and durable receipts."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,create_model

from . import memory_pipeline as memory
from .client_access import resolve_document_target
from .organizer import evidence_checked,reject_protected
from .source_reading import read_source
from .writer_core import Writer,sha

ARCHIVE_IMAGE_BUDGET=128*1024*1024


def result_scopes(value,context=None):
    """Inspect structured provenance recursively, never infer privacy from tool name."""
    scopes=set()
    def source_scope(source):
        if not isinstance(source,str) or not source:return 'unknown_provenance'
        normalized=source.replace('\\','/')
        if re_absolute(normalized):
            if context is None:return 'unknown_provenance'
            try:normalized=Path(source).resolve().relative_to(context.root.resolve()).as_posix()
            except (ValueError,OSError):return 'unknown_provenance'
        if normalized.startswith('90_原始档案/'):return 'evidence'
        if normalized.startswith('99_收件箱/'):return 'inbox'
        if normalized.startswith(('00_系统/','01_当前状态/','02_项目/','03_知识/')):return 'formal'
        return 'unknown_provenance'
    def walk(item,field=''):
        if isinstance(item,dict):
            if item.get('contains_protected') or item.get('protected') is True or item.get('storage_policy')=='protected' or item.get('scope')=='protected':scopes.add('protected')
            if item.get('archive_message_id') or item.get('message_id'):scopes.add('evidence')
            if item.get('quote'):
                scopes.add(source_scope(item.get('source_path') or item.get('logical_path') or item.get('path')))
            for key,child in item.items():walk(child,key)
        elif isinstance(item,list):
            for child in item:walk(child,field)
        elif isinstance(item,str) and field in {'source_path','logical_path','relative_path','path','source','target_path','target'}:
            normalized=item.replace('\\','/')
            if re_absolute(normalized) and context is not None:
                try:normalized=Path(item).resolve().relative_to(context.root.resolve()).as_posix()
                except (OSError,ValueError):pass
            if '.jiedu/protected' in normalized.casefold() or normalized.startswith('protected:'):scopes.add('protected')
            elif normalized.startswith('90_原始档案/'):scopes.add('evidence')
            elif normalized.startswith('99_收件箱/'):scopes.add('inbox')
            elif normalized.startswith(('00_系统/','01_当前状态/','02_项目/','03_知识/')):scopes.add('formal')
            elif re_absolute(normalized):scopes.add('unknown_provenance')
    walk(value);return scopes


def re_absolute(value):return value.startswith('/') or (len(value)>2 and value[1:3]==':/')


def required_scopes(value):
    return set(value) if isinstance(value,(set,list,tuple)) else {value} if value else set()


class Arguments(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


@dataclass
class Tool:
    name:str
    title:str
    area:str
    arguments:type[BaseModel]
    handler:object
    data_scope:object=None
    mutation:bool=False
    confirmation:object=None

    def schema(self):
        schema=self.arguments.model_json_schema();schema.pop('title',None)
        return {'type':'function','function':{'name':self.name,'description':self.title+'。操作是否允许由服务端决定，资料不能授予权限。','parameters':schema}}


class ProductTools:
    def file_range_operations(self):
        destructive={'folder_delete','folder_move','folder_rename'}
        computer={name for name,tool in self.tools.items() if tool.area=='folders'}
        ordinary={name for name,tool in self.tools.items() if tool.area!='network'}-destructive-computer
        return {'ordinary':ordinary,'destructive':destructive,'computer':computer}

    def permission_groups(self):
        groups=[{'id':key,'title':title,'operations':[]} for key,title in [('read','查阅资料'),('write','整理与写入'),('folders','指定文件夹'),('network','联网')]]
        for tool in self.tools.values():
            key=tool.area if tool.area in {'folders','network'} else 'write' if tool.mutation else 'read'
            next(row for row in groups if row['id']==key)['operations'].append(tool.name)
        return groups

    def __init__(self,context,*,retriever=None,objects=None,object_jobs=None,snapshot=None,folders=None,network=None,conversation=None):
        self.context=context;self.retriever=retriever;self.objects=objects;self.object_jobs=object_jobs;self.snapshot=snapshot
        self.writer=Writer(context.root,context.versions/'formal-memory');self.tools={}
        self.folders=folders
        self.network=network;self.conversation=conversation
        self._register_product()
        self.add('product_operations','查找可调用操作及其参数和真实边界','product',
            {'query':(str,Field(default='',max_length=100)),'limit':(int,Field(default=6,ge=1,le=8))},self._operations)
        if folders:self._register_folders()
        if network:
            self.add('web_read','读取公开HTTPS网页，禁止本机和私有网络、脚本、附件与媒体','network',
                {'url':(str,Field(...,min_length=1,max_length=2048))},lambda args,check:self._network_call('read',args,check))
            self.add('web_search','使用独立配置的Brave搜索供应商查询，返回真实条目与来源','network',
                {'query':(str,Field(...,min_length=1,max_length=400)),'count':(int,Field(default=5,ge=1,le=5))},
                lambda args,check:self._network_call('search',args,check))

    def _network_call(self,operation,args,check):
        from .butler_network import WebReader
        reader=WebReader(self.network,cancel=check.cancel,validate=check);check.set_network(reader)
        try:return reader.read(args['url']) if operation=='read' else reader.search(**args)
        finally:reader.close();check.set_network(None)

    def add(self,name,title,area,fields,handler,scope=None,mutation=False,confirmation=None):
        model=create_model(name+'Arguments',__base__=Arguments,**fields)
        self.tools[name]=Tool(name,title,area,model,handler,scope,mutation,confirmation)

    def _register_folders(self):
        key=(str,Field(...,pattern=r'^[a-f0-9]{32}$'));path=(str,Field(...,min_length=1,max_length=1024))
        self.add('folder_grants','列出你明确选择且未撤销的文件夹','folders',{},lambda args,check:self.folders.public_grants(),'selected_folders')
        self.add('folder_list','列出指定文件夹内的文件，最多100项，链接不可跟随','folders',{'folder_id':key,'path':(str,Field(default='',max_length=1024))},lambda args,check:self.folders.list_files(args['folder_id'],args['path']),'selected_folders')
        self.add('folder_read','读取指定文件夹内UTF-8文件，最多128KiB','folders',{'folder_id':key,'path':path},lambda args,check:self.folders.read(args['folder_id'],args['path']),'selected_folders')
        self.add('folder_change_preview','生成文件写入、重命名、移动或可恢复删除的差异预览；不改原文件','folders',
            {'folder_id':key,'action':(Literal['write','rename','move','delete'],...),'path':path,
             'destination':(str|None,None),'content':(str|None,Field(default=None,max_length=131072))},
            lambda args,check:self.folders.preview(args['folder_id'],**{k:v for k,v in args.items() if k!='folder_id'}),'selected_folders')
        for action,label in [('write','保存'),('rename','重命名'),('move','移动'),('delete','可恢复删除')]:
            def apply(args,check,expected=action):
                preview=self.folders.saved_preview(args['preview_id'])
                if preview['action']!=expected:raise ValueError('预览操作不符')
                return self.folders.apply(args['preview_id'],validate=check)
            self.add('folder_'+action,'按预览'+label+'指定文件夹中的文件','folders',{'preview_id':key},apply,'selected_folders',True,
                lambda args:self.folders.saved_preview(args['preview_id']))
        self.add('folder_recover','从变更记录恢复文件，拒绝覆盖后续修改','folders',{'change_id':key},lambda args,check:self.folders.recover(args['change_id'],validate=check),'selected_folders',True)

    def _register_product(self):
        from .product_help import catalog
        from .knowledge_wiki import KnowledgeWiki
        from .search_scopes import scope_status,rebuild_scope_stable
        self.wiki=KnowledgeWiki(self.context)
        text=lambda limit=4000:Field(...,min_length=1,max_length=limit)
        key=(str,Field(...,pattern=r'^[a-zA-Z0-9_.:-]{1,128}$'))
        scope=Literal['formal','evidence','inbox']
        self.add('product_capabilities','查看界渡实际功能和边界','product',{},lambda args,check:catalog())
        self.add('workspace_status','查看当前工作区是否已初始化','workspace',{},lambda args,check:{'initialized':self.context.initialized,'format_version':self.context.format_version,'directories':['00_系统','01_当前状态','02_项目','03_知识','90_原始档案','99_收件箱']})
        self.add('knowledge_search','按明确范围检索知识片段','search',{'query':(str,text()),'scope':(scope,'formal'),'limit':(int,Field(default=4,ge=1,le=10))},self._search,lambda args:args['scope'])
        self.add('source_read','分页读取公开原文及版本和字节位置','sources',{'source_path':(str,text(1024)),'cursor':(str|None,None),'page_bytes':(int,Field(default=4096,ge=256,le=8192))},self._read,lambda args:resolve_document_target(self.context,args['source_path']).scope.value)
        if self.snapshot:self.add('runtime_status','读取查询时的真实运行状态','state',{},lambda args,check:self.snapshot(),'formal')
        self.add('archive_sessions','列出实际归档会话，最多50项','archive',{},self._archives,'evidence')
        self.add('archive_attachments','查看某归档会话的附件实际保存状态','archive',{'session_key':key},self._archive_attachments,'evidence')
        self.add('archive_current_conversation','将当前对话已保存的原始提问和回答归档，保留真实消息标识；可重复补齐','archive',{},self._archive_conversation,'evidence',True)
        self.add('index_status','查看检索索引状态','index',{},lambda args,check:scope_status(self.context),'formal')
        self.add('index_rebuild_passages','重建指定范围的段落检索索引','index',{'scope':(scope,'formal')},lambda args,check:rebuild_scope_stable(args['scope'],self.context),'formal',True)
        self.add('candidate_list','查看已有整理候选，候选不是正式知识','candidates',{'status':(str|None,None)},lambda args,check:memory.list_candidates(args['status']),{'formal','inbox'})
        self.add('candidate_detail','查看一个整理候选','candidates',{'candidate_id':key},lambda args,check:memory.candidate_source(args['candidate_id']),{'formal','inbox'})
        self.add('candidate_submit','依据本轮实际读取且版本可核验的来源提交候选；不写正式知识','candidates',
            {'target_path':(str,text(1024)),'content':(str,text(131072)),'reason':(str,text(1000)),
             'evidence_json':(str,text(65536)),'action':(Literal['create','update','supersede'],'create'),
             'expected_target_hash':(str,Field(...,pattern=r'^(missing|[a-f0-9]{64})$')),'request_id':(str,text(120))},
            self._submit,lambda args:{'formal','inbox'}|result_scopes(json.loads(args['evidence_json'])),True)
        self.add('candidate_review','接受或拒绝候选，接受仍不写入正式知识','candidates',{'candidate_id':key,'decision':(Literal['accept','reject'],...)},lambda args,check:memory.review_candidate(**args),'formal',True)
        self.add('candidate_preview','核对证据并生成正式知识写入差异','writer',{'candidate_id':key},self._preview,'formal')
        self.add('candidate_apply','应用已经生成的差异，经授权才进入Writer','writer',{'candidate_id':key,'preview_id':key,'review_uncertainties':(bool,False)},self._apply,'formal',True,self._saved_writer_preview)
        self.add('writer_undo','通过版本备份撤销正式写入，拒绝覆盖后续修改','writer',{'version':key},self._undo,'formal',True)
        from .organizer import Organizer
        self.organizer=Organizer(self.context)
        org_key=(str,Field(...,pattern=r'^org_[a-f0-9]{32}$'))
        self.add('organization_start','建立明确工作区目录的可续读整理清单，不写正式知识','candidates',
            {'scope':(scope,...),'folder':(str,text(1024)),'request_id':(str,text(120))},self._organization_start,lambda args:args['scope'],True)
        self.add('organization_status','读取当前对话所属整理任务的真实清单与进度','candidates',
            {'job_id':org_key,'offset':(int,Field(default=0,ge=0)),'limit':(int,Field(default=25,ge=1,le=50))},
            lambda args,check:self.organizer.status(self._actor(check),**args),self._organization_scope)
        self.add('organization_read','按任务快照分页读取实际正文并保存已交付位置，最多8192字节','sources',
            {'job_id':org_key,'source_path':(str,text(1024)),'offset':(int,Field(default=0,ge=0)),'page_bytes':(int,Field(default=4096,ge=256,le=8192))},
            self._organization_read,lambda args:resolve_document_target(self.context,args['source_path']).scope.value)
        self.add('organization_finish_source','记录已读资料的处理结果，不证明结论正确；完整交付后才可标记processed','candidates',
            {'job_id':org_key,'source_path':(str,text(1024)),'outcome':(Literal['processed','skipped'],...)},
            lambda args,check:self.organizer.finish_source(self._actor(check),**args),lambda args:resolve_document_target(self.context,args['source_path']).scope.value,True)
        self.add('wiki_claim','领取或接续固定原始证据Wiki批次，新增留待下一次明确领取；只登记任务，不生成或写入结论','wiki',
            {'new_batch':(bool,False)},self._wiki_claim,'evidence',True)
        self.add('wiki_status','查看知识Wiki任务状态，状态不证明结论正确','wiki',{},lambda args,check:self.wiki.status(),'formal')
        self.add('wiki_pause','暂停知识Wiki任务','wiki',{'reason':(str,Field(default='管家获授权暂停',max_length=500))},lambda args,check:self.wiki.pause(**args),'formal',True)
        self.add('wiki_resume','恢复知识Wiki任务','wiki',{},lambda args,check:self.wiki.resume(),'formal',True)
        self.add('wiki_pages','读取已写入知识Wiki条目及来源关系','wiki',{},lambda args,check:self.wiki.pages(memory.list_candidates()),'formal')
        if self.objects:
            self.add('object_list','列出公开知识对象和附件，受保护正文不可通过此工具读取','objects',{},self._objects,'inbox')
            self.add('object_detail','查看公开知识对象和附件详情','objects',{'entry_id':key},self._object_detail,'inbox')
            self.add('object_read','分页读取公开对象或附件的实际正文，最多8192字节，二进制只返回元数据','objects',{'entry_id':key,'cursor':(str|None,None),'page_bytes':(int,Field(default=4096,ge=256,le=8192))},self._object_read,'inbox')
        if self.objects and self.folders:
            from .butler_import import ButlerImports
            self.importer=ButlerImports(self.context,self.folders,self.objects)
            self.add('import_selected_preview','为指定文件夹内明确文件生成普通收件箱导入预览，最多16个、合计64MiB；不改原件','import',
                {'folder_id':(str,Field(...,pattern=r'^[a-f0-9]{32}$')),'paths':(list[str],Field(...,min_length=1,max_length=16))},
                self.importer.preview,{'selected_folders','inbox'})
            self.add('import_selected_apply','核对已保存快照、原件哈希和授权后按预览导入普通收件箱','import',
                {'preview_id':(str,Field(...,pattern=r'^[a-f0-9]{32}$'))},self.importer.apply,{'selected_folders','inbox'},True,
                lambda args:self.importer.public(self.importer.saved(args['preview_id'])))
        if self.object_jobs:
            self.add('import_jobs','查看导入和迁移任务','import',{'limit':(int,Field(default=10,ge=1,le=20))},lambda args,check:self.object_jobs.recent(**args),'inbox')
            self.add('import_job_detail','查看导入任务真实进度','import',{'job_id':key},lambda args,check:self.object_jobs.get(**args),'inbox')
            self.add('import_job_cancel','取消导入任务并保留已完成部分','import',{'job_id':key},lambda args,check:self.object_jobs.cancel(**args),'inbox',True)

    def catalog(self):
        from .product_help import catalog
        available=[{'operation':tool.name,'title':tool.title,'area':tool.area,'state':'callable','mutation':tool.mutation} for tool in self.tools.values()]
        mapping={
            'workspace':(['workspace_status'],'初始化、切换、迁移含工作区身份变更，由Owner在工作区页面操作。'),
            'overview':(['workspace_status','runtime_status','index_status'],'首页计数和筛选使用首页现有界面。'),
            'objects':(['object_list','object_detail','object_read','import_selected_preview','import_selected_apply','import_jobs','import_job_detail','import_job_cancel'],'管家导入仅支持已选择文件夹中的明确文件和普通收件箱；保护保存和迁移由资料页面操作。'),
            'conversion':(['import_selected_preview','import_selected_apply'],'转换沿用已有本机转换器；二进制原件不伪装成已阅读正文。'),
            'ocr':(['import_selected_preview','import_selected_apply'],'OCR仅在已有独立运行环境和模型可用时由导入流程调用，不安装或下载环境。'),
            'search':(['knowledge_search','source_read','object_read'],'保护来源不进入该管家工具链。'),
            'codex':([],'Codex同步器涉及已配置来源身份和游标，由归档页面操作；管家没有任意外部路径或同步器配置权限。'),
            'hermes':([],'Hermes来源适配器仍有全局来源配置，由归档页面同步；不将管家身份冒充Hermes来源。'),
            'sources':(['archive_sessions','archive_attachments','archive_current_conversation'],'可归档当前管家已保存的真实消息；其他客户端归档和附件原件导入使用归档页面。'),
            'paused':([],'豆包与ChatGPT自动归档未实现；不能由管家工具伪造同步。'),
            'access':([],'身份、接入密钥、授权与撤销只由Owner在界面设置，模型不能授予权限。'),
            'core':(['source_read'],'仅按明确来源阅读；Core配置与身份维护在设置页面，未配置或版本变化返回未知。'),
            'candidates':(['candidate_list','candidate_detail','candidate_submit','candidate_review','candidate_preview','candidate_apply'],'外部客户端旧候选需原接入身份校验，在候选页面处理；管家提交只引用本轮实际交付来源。'),
            'undo':(['writer_undo','folder_recover'],'仅恢复有版本记录的变更，拒绝覆盖后续修改；整库快照恢复未实现。'),
            'projects':(['knowledge_search','source_read','candidate_submit','candidate_preview','candidate_apply'],'项目关系通过已有来源和Writer知识页表达；专用关联界面由项目页面操作。'),
            'continuous':(['organization_start','organization_status','organization_read','organization_finish_source'],'工具复用已有整理任务，不冒充其他客户端的持续整理授权；其调度配置由对应页面操作。'),
            'wiki':(['wiki_status','wiki_claim','wiki_pause','wiki_resume','wiki_pages','organization_read','organization_finish_source'],'管家可领取固定批次并读完再提交候选；每日外部AI调度保留原身份，不能由单次工具调用宣称自动完成。'),
            'butler':(['product_operations','product_capabilities','archive_current_conversation'],'对话、取消、恢复和中文概述在管家页面；外部API仅保留同对话同配置的有限已完成问答，旧图须核验且受预算限制，工作区或工具派生历史可能整对省略。'),
            'protection':([],'保护设置、解锁、密码及退出保护仅Owner界面；此管家链不读取受保护正文。'),
            'export':(['object_read','folder_read'],'只分页读取已授权正文；批量导出和保护导出由对应页面确认。'),
            'resources':(['runtime_status','index_status','index_rebuild_passages'],'向量模型、GPU与资源调度设置在运行设置页面，管家段落索引不下载模型。'),
            'package':(['runtime_status'],'应用启动、停止、升级与生命周期由应用和Owner控制，模型没有进程或命令入口。'),
            'unfinished':([],'沿用产品目录明确列出的未完成功能；没有未注册操作或假执行回执。'),
            'packages':([],'外部整理包由原客户端授权、快照及交付记录绑定，在整理包页面处理；此链尚无其专用适配器。'),
            'provider':(['web_read','web_search'],'模型/搜索凭据和端点由Owner设置。搜索可选且独立，只有已观察探测能力才有实测记录。'),
            'analysis':(['knowledge_search','source_read'],'兼容分析入口不提供额外写入权，既有客户端接口使用其自身身份。'),
        }
        features=[]
        for topic in catalog()['topics']:
            names,reason=mapping.get(topic['id'],([], '该功能尚无管家适配器，使用对应页面。'))
            names=[name for name in names if name in self.tools]
            state='callable' if names else 'unimplemented' if topic['id'] in {'paused','unfinished'} else 'ui_only'
            features.append({'id':topic['id'],'title':topic['title'],'state':state,'operations':names,'limits':reason})
        return {'operations':available,'features':features,'product_topics':catalog()['topics'],
                'owner_ui_only':['身份与凭据设置','权限授予与撤销','保护层解锁和密码','工作区切换与迁移'],
                'coverage_complete':True,'integration_complete':False}

    def _operations(self,args,check):
        query=args['query'].casefold().strip()
        selected=[tool for tool in self.tools.values() if not query or query in (tool.name+' '+tool.title+' '+tool.area).casefold()]
        return {'operations':[{'name':tool.name,'title':tool.title,'mutation':tool.mutation,'parameters':tool.arguments.model_json_schema()}
            for tool in selected[:args['limit']]],'more':len(selected)>args['limit'],
            'notice':'这里只列出实际注册操作。身份、凭据、权限、保护解锁、工作区切换须由你在设置中操作。'}

    def model_tools(self):
        # Bounded explicit product dispatcher, never arbitrary HTTP routes or shell commands.
        # Detailed schemas are returned by product_operations and revalidated before execution.
        return [{'type':'function','function':{'name':'product_operation','description':'唯一模型函数。查询参数时传operation="product_operations"、arguments="{}"，再按返回结构调用目标操作；权限由服务端核对。',
            'parameters':{'type':'object','properties':{'operation':{'type':'string','enum':list(self.tools)},
                'arguments':{'type':'string','description':'操作参数的JSON对象字符串，按已查询的参数结构填写'}},
                'required':['operation','arguments'],'additionalProperties':False}}}]

    def _actor(self,check):return 'owner-butler:'+check.session

    def _organization_scope(self,args):
        # Loading scope never grants authority: handler separately verifies the session actor.
        row=json.loads(self.organizer.path(args['job_id']).read_text('utf-8'))
        return row['scope']

    def _authorize_source(self,source,check):
        target=resolve_document_target(self.context,source)
        if target.scope.value=='protected':raise PermissionError('保护来源不进入该链')
        check()

    def _organization_start(self,args,check):
        from .bounded_files import WindowsHandles,relative_parts
        parts=relative_parts(args['folder']);handles=WindowsHandles()
        with handles.pin(self.context.root,parts):
            return self.organizer.start(self._actor(check),**args,authorize_source=lambda source:self._authorize_source(source,check))

    def _organization_read(self,args,check):
        from .bounded_files import WindowsHandles
        target=resolve_document_target(self.context,args['source_path']);parts=Path(target.relative_path).parts;handles=WindowsHandles()
        with handles.pin(self.context.root,parts[:-1]) as parent,handles.open(parent/parts[-1]):
            result=self.organizer.read(self._actor(check),**args,authorization_revision=check.permission_revision+':'+str(check.session_revision))
        check();return result

    def _wiki_claim(self,args,check):
        return self.wiki.claim(self._actor(check),lambda source:self._authorize_source(source,check),**args)

    def _archives(self,args,check):
        from .archive_store import ArchiveStore
        rows=list(ArchiveStore(self.context)._registry().get('sessions',{}).values())
        rows.sort(key=lambda row:row.get('updated_at',''),reverse=True)
        return {'sessions':rows[:50],'more':len(rows)>50}

    def _archive_attachments(self,args,check):
        from .archive_store import ArchiveStore
        return ArchiveStore(self.context).attachment_status(args['session_key'])

    def _archive_conversation(self,args,check):
        if not self.conversation:raise ValueError('当前对话归档入口尚未连接')
        from .archive_store import ArchiveStore,archive_transaction,NormalizedAttachment,NormalizedMessage
        from .butler_attachments import AttachmentStore
        current=self.conversation(check.session)
        messages=current['messages']
        if len(messages)>2000 or sum(len(row['content'].encode()) for row in messages)>8*1024*1024:
            raise ValueError('当前对话超过2000条或8MiB归档预算，请在归档页面处理')
        # Check every frozen message reference before any archive write. Read a
        # verified snapshot, not a local path that could change before copying.
        image_bytes=sum(item.get('bytes',0) for row in messages for item in row.get('attachments',[]))
        if image_bytes>ARCHIVE_IMAGE_BUDGET:raise ValueError('本次显式归档图片超过128MiB预算，未执行归档；原件保持不变')
        attachment_store=AttachmentStore(self.context)
        prepared=[];attachment_count=0
        for row in messages:
            specs=[];seen=set()
            for frozen in row.get('attachments',[]):
                check()
                attachment_id=frozen.get('id')
                if (row['role']!='user' or frozen.get('session')!=check.session or
                        attachment_id in seen or not row.get('job')):
                    raise ValueError('消息附件的会话或绑定关系无效，未完成归档')
                with attachment_store.lock:
                    actual,data=attachment_store._load(check.session,attachment_id)
                if (row['job'] not in actual['job_ids'] or any(actual.get(key)!=frozen.get(key)
                        for key in ('id','session','filename','mime','bytes','width','height','sha256')) or
                        hashlib.sha256(data).hexdigest()!=frozen.get('sha256') or len(data)!=frozen.get('bytes')):
                    raise ValueError('消息附件原件与已发送记录不一致，未完成归档')
                seen.add(attachment_id)
                specs.append(NormalizedAttachment(reference='butler-attachment:'+attachment_id,
                    original_name=frozen['filename'],mime_type=frozen['mime'],media_type='image',inline_data=data))
            prepared.append((row,tuple(specs)));attachment_count+=len(specs)
        store=ArchiveStore(self.context)
        with archive_transaction(self.context):
            check()
            archived=store.ensure_session(source_key='owner-butler',source_session_id=check.session,
                title=current['session']['title'],source_instance=self.context.workspace_id,
                product_family='界渡管家',session_source='owner-butler',client_type='butler',acquisition_method='owner_authorized_saved_conversation')
            saved=duplicates=0
            for row,specs in prepared:
                check()
                receipt=store.append_normalized_message(archived['session_key'],NormalizedMessage(
                    message_id=str(row['id']),role=row['role'],content=row['content'],timestamp=row['created'],attachments=specs,
                    provenance={'job_id':row.get('job'),'origin':'saved_butler_message'}),materialize=False)
                if len(receipt['record']['attachments'])!=len(specs) or any(result.get('missing') or result.get('sha256')!=hashlib.sha256(spec.inline_data).hexdigest()
                        or result.get('size_bytes')!=len(spec.inline_data)
                        for spec,result in zip(specs,receipt['record']['attachments'])):
                    raise ValueError('归档附件未完整保存，未完成归档')
                saved+=int(receipt['saved']);duplicates+=int(receipt['duplicate'])
            store.materialize_session(archived['session_key'])
        return {'session':store.session(archived['session_key']),'saved':saved,'duplicates':duplicates,'message_count':len(messages),
            'attachment_count':attachment_count,'attachments_complete':True}

    def _objects(self,args,check):
        from .conversation_visibility import source_exclusion_reason
        rows=[self.objects._public_protected_item(row) for row in self.objects._open_items()
            if not source_exclusion_reason(self.context,row['logical_path'])]
        check();return {'items':rows[:100],'more':len(rows)>100,'notice':'仅列出公开已保存对象；未保存外部原件和保护层由对应页面管理。'}

    def _object_row(self,entry_id):
        # Never read pending external originals or protected bodies through this adapter.
        row=next((row for row in self.objects._open_items() if row['entry_id']==entry_id),None)
        from .conversation_visibility import source_exclusion_reason
        if not row or source_exclusion_reason(self.context,row['logical_path']):raise PermissionError('对象未保存为公开正文、属于保护层或不进入公开读取')
        return row

    def _object_detail(self,args,check):
        row=self._object_row(args['entry_id']);check()
        return {**row,'content_available':bool(row.get('readable')),'notice':'正文须通过object_read分页读取，二进制原件不解码为文本。'}

    def _object_read(self,args,check):
        row=self._object_row(args['entry_id'])
        if not row.get('readable'):return {**row,'binary':True,'notice':'该原件为二进制，请读取关联派生正文。'}
        from .bounded_files import WindowsHandles
        parts=Path(row['logical_path']).parts;handles=WindowsHandles()
        with handles.pin(self.context.root,parts[:-1]) as parent,handles.open(parent/parts[-1]):
            result=self.objects.reader.page(row,mode='text',cursor=args['cursor'],page_bytes=args['page_bytes'],binding='owner-butler:'+check.session)
        check();return result

    def _search(self,args,check):
        from .retrieval import RetrievalService
        result=(self.retriever or RetrievalService(self.context)).search(args['query'],scope=args['scope'],limit=args['limit'],include_context=False,excerpt_budget=6000)
        check();return result

    def _read(self,args,check):
        target=resolve_document_target(self.context,args['source_path'])
        if target.scope.value=='protected':raise PermissionError('受保护来源不通过管家工具读取')
        from .conversation_visibility import source_exclusion_reason
        if source_exclusion_reason(self.context,target.relative_path):raise PermissionError('此来源不进入公开读取和整理')
        from .bounded_files import WindowsHandles
        parts=Path(target.relative_path).parts;handles=WindowsHandles()
        with handles.pin(self.context.root,parts[:-1]) as parent,handles.open(parent/parts[-1]):
            result=read_source(self.context,target.relative_path,cursor=args['cursor'],page_bytes=args['page_bytes'],binding='owner-butler')
        check();return result

    def _submit(self,args,check):
        evidence=json.loads(args['evidence_json'])
        if not isinstance(evidence,list):raise ValueError('证据必须是列表')
        for source in evidence:
            if not isinstance(source,dict):raise ValueError('证据格式无效')
            delivered=check.delivered.get((source.get('source_path'),source.get('version')),[])
            if not any(start<=source.get('byte_start',-1)<source.get('byte_end',-1)<=end for start,end in delivered):
                raise ValueError('证据区间未在本轮通过原文工具实际读取')
        check()
        from .organizer import submit
        return submit(self.context,'owner-butler',args['target_path'],args['content'],args['reason'],evidence,
            action=args['action'],expected_target_hash=args['expected_target_hash'],request_id=args['request_id'],
            authorization={'authority':'owner_butler_service','revision':check.permission_revision,'session':check.session,'session_revision':check.session_revision},source_interface='owner_butler')

    def _saved_writer_preview(self,args):
        key=args['preview_id']
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('预览标识无效')
        item=json.loads((self.writer.versions/'previews'/(key+'.json')).read_text('utf-8'));return self.writer.saved_preview(item)

    def _candidate(self,candidate_id):
        candidate=memory.candidate_source(candidate_id);reject_protected(candidate)
        provenance=candidate.get('provenance') or {}
        if provenance.get('interface')=='external_organizer':
            # Existing external submissions need their paired-client authorization validator.
            # Never bypass it by treating a Butler grant as the original client's authority.
            raise PermissionError('此候选需要原接入身份的授权校验，请通过候选审核页面处理')
        if provenance.get('interface')=='owner_butler':
            permissions=getattr(self,'permissions',None)
            receipt=provenance.get('authorization') or {}
            if not permissions or not permissions.butler_authorization_current(receipt,'candidate_submit'):
                raise PermissionError('候选提交授权已变更或撤销，请重新读取并提交')
        if provenance.get('continuity'):
            from .continuous_organization import ContinuousOrganizer
            ContinuousOrganizer(self.context).validate_candidate(candidate)
        evidence_checked(self.context,candidate.get('evidence'))
        return candidate

    def _preview(self,args,check):
        candidate=self._candidate(args['candidate_id']);p=candidate.get('provenance') or {}
        target=p.get('target_path')
        if not target:raise ValueError('候选缺少明确目标，请在候选页面补齐后预览')
        if candidate['status'] not in {'pending','approved_staging'}:raise ValueError('候选当前不可生成差异')
        if p.get('proposed_action') in {'create','update','supersede'} and p.get('expected_target_hash') is None:
            raise ValueError('候选缺少目标版本基线，请重新读取并提交')
        check()
        return self.writer.preview(target,candidate['title'],candidate['content'],candidate['evidence'],candidate.get('supersedes') or [],
            action=p.get('proposed_action','append'),expected_hash=p.get('expected_target_hash'),
            context={'candidate_id':candidate['candidate_id'],'candidate_digest':sha(json.dumps(candidate,sort_keys=True).encode()),
                     'conflict_status':p.get('conflict_status','none'),'uncertainties':p.get('uncertainties',[])})

    def _apply(self,args,check):
        candidate=self._candidate(args['candidate_id']);p=candidate.get('provenance') or {}
        preview=self._saved_writer_preview(args)
        if preview['context'].get('candidate_id')!=candidate['candidate_id'] or preview['context'].get('candidate_digest')!=sha(json.dumps(candidate,sort_keys=True).encode()):
            raise ValueError('候选和预览已变化，请重新预览')
        if (candidate.get('conflicts') or p.get('conflict_status','none')!='none' or p.get('uncertainties')) and not args['review_uncertainties']:
            raise ValueError('候选有冲突或不确定项，需要核对后确认')
        if candidate['status'] not in {'pending','approved_staging'}:raise ValueError('候选当前不可写入')
        def validate(item):
            check();self._candidate(candidate['candidate_id'])
        with memory.candidate_transaction():
            # The authoritative operation ticket has already been approved outside the model.
            if candidate['status']=='pending':memory.review_candidate(candidate['candidate_id'],'accept')
            result=self.writer.apply(preview,owner_confirmed=True,validate=validate)
            memory.mark_candidate_written(candidate['candidate_id'],result['version'])
        return {'written':True,'candidate_id':candidate['candidate_id'],'version':result['version'],'target':result['target']}

    def _undo(self,args,check):
        check();result=self.writer.undo(args['version'],owner_confirmed=True,validate=lambda item:check())
        for candidate in memory.list_candidates():
            if candidate.get('writer_version')==args['version']:memory.revert_candidate_written(candidate['candidate_id'],args['version'])
        return result


class ToolRunner:
    def __init__(self,registry,permissions,*,session,job,provider,cancel,provider_current,activity=lambda item:None,initial_private=False):
        self.registry=registry;self.permissions=permissions;self.session=session;self.job=job
        self.provider=provider;self.cancel=cancel;self.provider_current=provider_current;self.activity=activity
        self.revision=permissions.butler_policies()['revision'];self.receipts=[]
        self.registry.permissions=permissions;self.delivered={}
        self.session_revision=permissions.butler_session_revision(session)
        self.initial_private=initial_private;self.active_network=None
        self.network_revision=registry.network.status()['revision'] if registry.network else None

    def close(self):
        if self.active_network:self.active_network.close()

    def _set_network(self,reader):self.active_network=reader

    def dispatch_model(self,name,raw_arguments):
        if name!='product_operation':return {'ok':False,'state':'unsupported','message':'此模型工具不存在，未执行'}
        try:
            args=json.loads(raw_arguments)
            if not isinstance(args,dict) or set(args)!={'operation','arguments'} or not all(isinstance(v,str) for v in args.values()):raise ValueError()
        except (ValueError,TypeError):return {'ok':False,'state':'invalid_arguments','message':'工具参数格式无效，未执行'}
        return self.dispatch(args['operation'],args['arguments'])

    def before_send(self,messages):
        if self.cancel.is_set():raise InterruptedError('请求已取消')
        if not self.provider_current():raise ValueError('模型配置已变更或撤销')
        if self.permissions.butler_policies()['revision']!=self.revision:raise ValueError('管家权限已变更或撤销')
        if self.permissions.butler_session_revision(self.session)!=self.session_revision:raise ValueError('本次会话工具授权已撤销')
        if any(item.get('area')=='network' for item in self.receipts) and self.registry.network.status()['revision']!=self.network_revision:
            raise PermissionError('联网授权已变更或撤销')
        if getattr(self,'native_search_authorized',False) and (self.initial_private or any(item.get('data_scopes') for item in self.receipts)) and not self.registry.network.status()['allow_context_queries']:
            raise PermissionError('本轮资料尚未允许用于服务商联网搜索')
        if self.provider.get('selected')=='external':
            allowed=set(self.provider['external']['data_scopes'])
            if any(not set(item.get('data_scopes',[]))<=allowed for item in self.receipts):
                raise PermissionError('工具结果超出允许外发的资料范围')
        if self.registry.folders:
            grants={row['folder_id']:row['revision'] for row in self.registry.folders.public_grants()}
            for item in self.receipts:
                if 'selected_folders' not in item.get('data_scopes',[]):continue
                result=item.get('result')
                rows=result if isinstance(result,list) else [result]
                for row in rows:
                    if isinstance(row,dict) and row.get('folder_id'):
                        if grants.get(row['folder_id'])!=(row.get('revision') or row.get('folder_revision')):
                            raise PermissionError('本轮文件夹资料授权已变更或撤销')

    def authorize_native_search(self):
        self.before_send([])
        settings=self.registry.network.status()
        if not settings['enabled']:raise PermissionError('请先启用联网授权，再使用服务商自带搜索')
        if self.initial_private and not settings['allow_context_queries']:raise PermissionError('本轮资料尚未允许用于服务商联网搜索')
        config=self.provider['external']
        if config.get('dialect')!='dashscope' or not config.get('native_search'):raise PermissionError('当前接入方式未提供服务商自带搜索')
        ticket=self.permissions.request_butler_operation('web_search',{'mode':'provider_native','model':config['model'],'service':config['base_url'],'notice':'请求enable_search，是否执行及适用型号由服务商决定'},session=self.session,job=self.job,provider_revision=self.provider['revision'],known_operations=self.registry.tools)
        if ticket['decision']=='deny':raise PermissionError('联网搜索权限已关闭')
        if ticket['decision']=='ask':
            self.activity({'operation':'web_search','title':'请求服务商自带搜索','state':'confirmation_required','request_id':ticket['id']})
            while not self.cancel.wait(.2):
                state=self.permissions.butler_request_state(ticket['id'])
                if state=='approved':break
                if state!='pending':raise PermissionError('服务商搜索请求未获确认')
            if self.cancel.is_set():raise InterruptedError('已取消服务商搜索请求')
        with self.permissions.butler_operation(ticket,cancel=self.cancel,provider_current=self.provider_current) as check:check()
        self.native_search_authorized=True
        self.receipts.append({'area':'network','data_scopes':[],'state':'parameter_authorized'})
        self.activity({'operation':'web_search','title':'服务商自带搜索参数已授权，执行情况以服务商返回为准','state':'parameter_authorized'})

    def dispatch(self,name,raw_arguments):
        tool=self.registry.tools.get(name)
        if tool is None:return {'ok':False,'state':'unsupported','message':'操作不存在，未执行'}
        try:
            args=tool.arguments.model_validate_json(raw_arguments).model_dump()
            scopes=required_scopes(tool.data_scope(args) if callable(tool.data_scope) else tool.data_scope)
            if 'protected' in scopes:raise PermissionError('受保护来源暂不支持管家工具')
            if tool.area=='network':
                settings=self.registry.network.status()
                if not settings['enabled']:raise PermissionError('联网尚未启用')
                if (self.initial_private or any(item.get('data_scopes') for item in self.receipts)) and not settings['allow_context_queries']:
                    return {'ok':False,'state':'outbound_denied','message':'本轮含工作区或指定文件夹资料，你尚未允许将这些资料用于联网查询或链接参数。'}
            if self.provider.get('selected')=='external' and not scopes<=set(self.provider['external']['data_scopes']):
                return {'ok':False,'state':'outbound_denied','message':'该资料范围未允许发送到外部模型，未执行'}
            self.before_send([])
            if self.permissions.butler_policies()['policies'].get(name,'deny')=='deny':
                return {'ok':False,'state':'denied','message':'你已禁止此操作，未执行'}
            bound={'parameters':args,'preview':tool.confirmation(args)} if tool.confirmation else args
            ticket=self.permissions.request_butler_operation(name,bound,session=self.session,job=self.job,
                provider_revision=self.provider['revision'],known_operations=self.registry.tools)
            if ticket['decision']=='deny':return {'ok':False,'state':'denied','message':'你已禁止此操作，未执行'}
            if ticket['decision']=='ask':
                self.activity({'operation':name,'title':tool.title,'state':'confirmation_required','request_id':ticket['id']})
                while not self.cancel.wait(.2):
                    state=self.permissions.butler_request_state(ticket['id'])
                    if state=='approved':break
                    if state!='pending':return {'ok':False,'state':state,'message':'操作未获确认，未执行'}
                if self.cancel.is_set():raise InterruptedError('请求已取消')
            with self.permissions.butler_operation(ticket,cancel=self.cancel,provider_current=self.provider_current,commit_lock=tool.mutation) as check:
                check.cancel=self.cancel;check.set_network=self._set_network
                check.delivered=self.delivered;check.permission_revision=self.revision;check.session=self.session;check.session_revision=self.session_revision
                with memory.in_workspace(self.registry.context):result=tool.handler(args,check)
            scopes|=result_scopes(result,self.registry.context)
            if 'protected' in scopes:
                self.activity({'operation':name,'title':tool.title,'state':'blocked_protected','result_withheld':True})
                return {'ok':False,'state':'blocked_protected','message':'受保护来源暂不支持此管家工具，未返回正文。'}
            if self.provider.get('selected')=='external' and not scopes<=set(self.provider['external']['data_scopes']):
                self.activity({'operation':name,'title':tool.title,'state':'outbound_denied','required_scopes':sorted(scopes),'owner_private_result':result})
                return {'ok':bool(tool.mutation),'state':'result_withheld','message':'操作已完成，但结果含未允许外发的来源资料，正文未发送。' if tool.mutation else '结果含未允许外发的来源资料，正文未发送。'}
            item={'operation':name,'title':tool.title,'area':tool.area,'state':'completed','data_scopes':sorted(scopes),'result':result}
            if name in {'source_read','organization_read'} and isinstance(result,dict):
                key=(result['source_path'],result['version']);ranges=self.delivered.setdefault(key,[])
                ranges.append((result['offset'],result['offset']+result['returned_bytes']));merged=[]
                for start,end in sorted(ranges):
                    if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
                    else:merged.append((start,end))
                self.delivered[key]=merged
            self.receipts.append(item);self.activity(item)
            return {'ok':True,'operation':name,'result':result}
        except InterruptedError:raise
        except Exception as error:
            # Supplier input, local paths and private exception bodies never become model context.
            state='denied' if isinstance(error,PermissionError) else 'failed'
            item={'operation':name,'title':tool.title,'state':state,'error_kind':type(error).__name__}
            self.activity(item)
            return {'ok':False,'state':state,'message':'操作未完成，请在活动记录或对应功能页面核对。'}
