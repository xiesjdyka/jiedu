"""One scoped passage contract for UI, MCP and direct clients."""
import json
from pathlib import Path
from .search_scopes import search_scope
from .workspace import active_workspace

MODES={'keyword','vector','hybrid'}
SCOPES={'formal','evidence','inbox','workspace'}
FORMAL_PREFIXES=('00_系统/','01_当前状态/','02_项目/','03_知识/')

class DirectoryRetrievalContext:
    def __init__(self,root):
        self.root=Path(root).resolve();self.formal_roots=tuple(self.root/n for n in ('00_系统','01_当前状态','02_项目','03_知识'))
        self.raw_archive=self.root/'90_原始档案';self.inbox=self.root/'99_收件箱';self.initialized=True
        self.workspace_id=None
    def relative(self,path):return Path(path).resolve().relative_to(self.root).as_posix()
    def index_root(self,scope):return self.root/'.jiedu'/'index'/scope

class RetrievalService:
    def __init__(self,context=None,*,vector_backend=True,vector_backend_reason=None):
        self.context=context or active_workspace();self.vector_backend=vector_backend;self.vector_backend_reason=vector_backend_reason
        if not self.context.initialized:raise ValueError('检索需要已初始化工作区')
    @classmethod
    def for_root(cls,root):return cls(DirectoryRetrievalContext(root),vector_backend=False)
    def search(self,query,*,mode='hybrid',scope='formal',limit=10,source_type=None,folder=None,project=None,include_context=True,candidate_budget=80,excerpt_budget=6000):
        if mode not in MODES or scope not in SCOPES:raise ValueError('无效检索模式或范围')
        limit=max(1,min(int(limit),50));candidate_budget=max(limit,min(200,int(candidate_budget)));excerpt_budget=max(256,min(12000,int(excerpt_budget)))
        vector=[];reason=None;embedding_ran=False;model_state=None;freshness=None
        if mode!='keyword':
            if scope=='inbox':reason='收件箱使用本地段落检索'
            elif not self.vector_backend:reason=self.vector_backend_reason or '该目录调用未连接向量索引'
            else:
                from . import core
                backend=core
                if scope=='evidence':
                    from . import evidence_index as backend
                elif scope=='workspace':
                    from . import workspace_vector as backend
                model_state=core.local_model_status()
                if Path(core.VAULT).resolve()!=self.context.root.resolve():reason='向量索引工作区不匹配'
                elif model_state['state']=='unavailable':reason='本机没有完整的向量模型缓存，已回退段落检索'
                elif model_state['state'] in {'available_unloaded','loading'}:
                    core.start_model_warmup();model_state=core.local_model_status();reason='本机向量模型正在后台加载，本次先使用段落检索；后续查询会自动启用语义检索'
                elif model_state['state']=='load_failed':reason='本机向量模型加载失败，已回退段落检索'
                elif core.model_busy():reason='后台索引或其他请求占用共享向量模型，本次使用段落检索；未等待后台索引完成'
                else:
                    freshness=(backend.index_freshness() if scope=='formal' else
                               backend.status(self.context) if scope=='workspace' else backend.freshness(self.context))
                    active_table=backend.table(self.context) if scope in {'evidence','workspace'} else backend.table()
                    if scope=='evidence' and active_table is not None and not freshness['fresh']:
                        backend.start_sync(self.context,operation='console_query_evidence_incremental')
                    if active_table is None:reason=('正式' if scope=='formal' else '本机资料' if scope=='workspace' else '证据')+'向量索引尚未建立'
                    elif scope=='formal' and not freshness['fresh']:reason='正式向量索引不是当前版本，已回退段落检索；请先完成增量同步'
                    elif scope=='workspace' and not freshness['ready']:reason='本机资料向量索引不是当前版本，已回退关键词检索；请更新索引'
                    else:
                        try:
                            query_vector=core.embed([query],local_only=True,lock_timeout=.1)[0];embedding_ran=True
                            vector=(core._vector_rows(query,query_vector,candidate_budget,source_type,folder,project)
                                    if scope=='formal' else backend.vector_rows(query_vector,candidate_budget,self.context,source_type,folder,project))
                            model_state=core.local_model_status()
                        except core.ModelBusyError:
                            model_state=core.local_model_status();reason='后台索引或其他请求占用共享向量模型，本次使用段落检索；模型等待已限制为100毫秒'
                        except Exception:
                            model_state=core.local_model_status();reason='本地向量模型加载或检索失败，已回退段落检索'
        result=search_scope(query,scope,limit,self.context,candidate_budget=candidate_budget,excerpt_budget=excerpt_budget,folder=folder,project=project,source_type=source_type,vector_hits=vector,requested_mode=mode)
        from .knowledge_entities import KnowledgeEntities
        entities=KnowledgeEntities(self.context)
        entity=(entities.resolve(name=project) if project else None) or entities.mentioned(query)
        if scope=='formal' and entity:result['entity_context']=entities.public(entity)
        for row in result.get('results',[]):
            matched=entities.resolve(path=row.get('source_path'))
            if matched:
                row['entity_context']=entities.public(matched)
                row['historical_project']=row.get('project');row['project']=matched['name']
        effective=mode if vector and result.get('vector_matched_passages') else 'keyword'
        if vector and effective=='keyword':reason='向量候选未定位到当前版本正文，已使用段落检索'
        result.update(requested_mode=mode,effective_mode=effective,vector_used=effective!='keyword',embedding_used=embedding_ran,fallback_reason=reason,
                      retrieval_backend={"scope":scope,"vector_index_available":scope in {'formal','evidence','workspace'} and self.vector_backend,
                                         "model":model_state,"index_freshness":freshness,
                                         "attachments":"只检索已生成且在所选范围内的可读派生文本"})
        for row in result['results']:row.update(search_method=effective,fallback_reason=reason)
        if scope=='evidence' and freshness and not freshness.get('fresh'):
            result['freshness_note']='活动归档增量正在后台合并；本次向量候选仍以当前原文再定位，已变化的旧片段不会直接返回。'
        result['observability']={'keyword_candidates':result['matched_passages'],'vector_candidates':len(vector),'context_candidates':result.get('context_passages',0),'hybrid_candidates':result['candidate_count'],'deduplicated_candidates':result['candidate_count'],'returned':result['returned'],'requested_limit':limit}
        result['budgets']['response_bytes_limit']=65536
        result['results_truncated']=len(result['results'])<result['candidate_count']
        while len(json.dumps(result,ensure_ascii=False).encode('utf-8'))>65000 and result['results']:
            result['results'].pop();result['results_truncated']=True
        result['returned']=len(result['results']);result['observability']['returned']=result['returned']
        result['budgets']['returned_characters']=sum(len(r['content']) for r in result['results'])
        result['budgets']['context_returned_characters']=sum(len(item.get('content','')) for row in result['results'] for item in row.get('context',{}).get('messages',row.get('context',{}).get('sections',[])))
        result['observability']['context_candidates']=sum(len(row.get('context',{}).get('messages',row.get('context',{}).get('sections',[]))) for row in result['results'])
        result['budgets']['serialized_bytes_before_size_field']=len(json.dumps(result,ensure_ascii=False).encode('utf-8'))
        return result

def retrieve(query,**kwargs):return RetrievalService().search(query,**kwargs)
