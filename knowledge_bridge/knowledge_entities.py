"""One read-only entity/alias projection shared by Wiki, context and retrieval.

The workspace sidecar records explicit user classification; original documents,
historical facts and search indexes remain untouched.
"""
import json,re
from pathlib import Path

class KnowledgeEntities:
    def __init__(self,context):
        self.root=Path(context.root);self.path=self.root/'.jiedu/state/knowledge-entities.json';self.entities=[]
        try:
            if self.path.stat().st_size>262144:return
            data=json.loads(self.path.read_text('utf-8-sig'))
            if data.get('schema_version')!=1 or data.get('workspace_id')!=getattr(context,'workspace_id',None):return
            rows=data.get('entities',[])
            if not isinstance(rows,list) or len(rows)>500:return
            ids=set();aliases=set();paths=set()
            for row in rows:
                if not isinstance(row,dict) or not row.get('entity_id') or not row.get('name'):return
                if row['entity_id'] in ids:return
                names=[row['name'],*row.get('aliases',[])]
                if any(not isinstance(n,str) or not n or len(n)>160 for n in names):return
                if aliases.intersection(n.casefold() for n in names):return
                for prefix in row.get('paths',[]):
                    p=Path(prefix)
                    if p.is_absolute() or '..' in p.parts or not prefix.startswith(('02_项目/','03_知识/')) or prefix in paths:return
                    paths.add(prefix)
                ids.add(row['entity_id']);aliases.update(n.casefold() for n in names)
            self.entities=rows
        except (OSError,ValueError,TypeError,AttributeError):pass
    def resolve(self,name=None,path=None):
        rows=[r for r in self.entities if (name and name.casefold() in [n.casefold() for n in [r['name'],*r.get('aliases',[])]]) or (path and any(path==p or path.startswith(p.rstrip('/')+'/') for p in r.get('paths',[])))]
        return rows[0] if len(rows)==1 else None
    def mentioned(self,text):
        rows=[r for r in self.entities if any(n in str(text) for n in [r['name'],*r.get('aliases',[])])]
        return rows[0] if len(rows)==1 else None
    def public(self,row):
        current=row.get('current_state') or {}
        confirmed=current.get('basis')=='owner_instruction' and bool(current.get('confirmed_at')) and bool(current.get('source_reference'))
        return {'entity_id':row['entity_id'],'name':row['name'],'kind':row.get('kind','project'),'aliases':row.get('aliases',[]),'paths':row.get('paths',[]),'phases':row.get('phases',[]),'relationship_note':row.get('relationship_note'),'status':current.get('text') if confirmed else '最近状态待核实','status_basis':'owner_confirmed' if confirmed else 'unknown','confirmed_at':current.get('confirmed_at') if confirmed else None,'state_source':current.get('source_reference') if confirmed else None,'source':'.jiedu/state/knowledge-entities.json'}
    def detail(self,name):
        row=self.resolve(name=name)
        if not row:return None
        result=self.public(row)
        return {**result,'project':row['name'],'requested_name':name,'summary':result['status'],'current_state':result['status'],'overview':row.get('relationship_note') or '', 'overview_source':result['source'],'updated_at':result['confirmed_at'],'entity_projection':True,'historical_paths':row.get('paths',[]),'implemented':None,'unfinished':None,'blocked':None,'next_step':None}

def facts(text):
    """Declared historical date only; mtime and updated are never event dates."""
    header=re.match(r'\A---\s*\n(.*?)\n---\s*\n',text,re.S);meta={}
    if header:
        for line in header[1].splitlines():
            key,sep,value=line.partition(':')
            if sep:meta[key.strip()]=value.strip().strip('"\'')
        text=text[header.end():]
    date=meta.get('valid_from') or meta.get('date')
    if date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?',date):date=None
    return {'fact_date':date,'document_role':meta.get('document_role'),'temporal_label':'历史记录 · '+date[:10] if date else '记录时间待核实','body':text.strip()}
