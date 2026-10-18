import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import session_archive as archive, manual_extraction as manual
from knowledge_bridge.workspace import initialize_workspace


class AliasSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ctx=initialize_workspace(Path(self.temp.name)/'vault')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(archive,SESSIONS_PATH=self.ctx.sessions_state,
            ARCHIVE_ROOT=self.ctx.raw_message_store,CANONICAL_ROOT=self.ctx.raw_archive))
        self.stack.enter_context(patch.multiple(manual,SESSIONS_PATH=self.ctx.sessions_state,ARCHIVE_ROOT=self.ctx.raw_message_store))
        for module in (archive,manual):self.stack.enter_context(patch.object(module,'active_workspace',return_value=self.ctx))
        self.stack.enter_context(patch('knowledge_bridge.original_archive_import.session_records',return_value=[]))
        self.stack.enter_context(patch.object(archive,'materialize',side_effect=AssertionError('read must not publish')))
        self.old=self.make('old','hermes:stable',[self.message('old','historical')])
        self.new=self.make('new','canonical-key',[self.message('new','current')],source='hermes:stable')
        self.ctx.sessions_state.write_text(json.dumps({'sessions':{'hermes:stable':{'client_id':'hermes','source':'desktop'}}}),'utf-8')
        self.live=archive._archive_path('hermes:stable',self.ctx.raw_message_store)
        self.live.parent.mkdir(parents=True,exist_ok=True)
        self.write_live([self.message('new','current')])

    @staticmethod
    def message(mid,text):return {'message_id':mid,'role':'user','original_text':text,'attachments':[{'reference':'image','status':'missing'}]}

    def make(self,name,sid,messages,source=None):
        folder=self.ctx.raw_archive/'Hermes'/name;folder.mkdir(parents=True)
        manifest={'session_id':sid,'client':'hermes','session_title':'same title'}
        if source:manifest.update(session_key=sid,source_session_id=source)
        (folder/'manifest.json').write_text(json.dumps(manifest),'utf-8')
        (folder/'messages.jsonl').write_text(''.join(json.dumps(m)+'\n' for m in messages),'utf-8')
        (folder/'conversation.md').write_text('\n'.join(m['original_text'] for m in messages),'utf-8')
        return folder

    def write_live(self,rows):self.live.write_text(''.join(json.dumps(m)+'\n' for m in rows),'utf-8')
    def items(self):return {x['session_id']:x for x in archive.list_archives(read_only=True,include_conversation=False)}
    def fingerprints(self):return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.ctx.root.rglob('*') if p.is_file()}

    def test_order_independent_preserves_history_and_current_once(self):
        before=self.fingerprints();paths=[self.old/'manifest.json',self.new/'manifest.json']
        for order in (paths,list(reversed(paths))):
            with patch.object(archive,'_manifest_paths',return_value=order):
                items=self.items()
            self.assertEqual(2,len(items));self.assertEqual('historical',items['hermes:stable']['snapshot_kind'])
            self.assertEqual('historical',items['hermes:stable']['messages'][0]['original_text'])
            self.assertTrue(items['canonical-key']['live_snapshot_matched'])
            self.assertEqual('current',items['canonical-key']['messages'][0]['original_text'])
            self.assertEqual([{'reference':'image','status':'missing'}],items['hermes:stable']['messages'][0]['attachments'])
        self.assertEqual(before,self.fingerprints())

    def test_same_id_changed_text_does_not_overwrite_either_snapshot(self):
        self.write_live([self.message('new','changed')]);items=self.items()
        self.assertTrue(items['canonical-key']['live_snapshot_pending'])
        self.assertEqual('current',items['canonical-key']['messages'][0]['original_text'])
        self.assertEqual('historical',items['hermes:stable']['messages'][0]['original_text'])

    def test_new_live_message_remains_pending_not_reviewed(self):
        self.write_live([self.message('new','current'),self.message('later','new message')])
        self.receipt()
        status=manual.status(state_path=self.ctx.state/'manual.json',state_sessions=self.ctx.sessions_state,archive_root=self.ctx.raw_message_store)
        current=next(r for r in status['pending_sessions'] if r['session_id']=='canonical-key')
        self.assertEqual('changed',current['review_state']);self.assertEqual(0,status['reviewed_session_count'])

    def receipt(self):
        path=self.new/'conversation.md';raw=path.read_bytes()
        (self.ctx.state/'organization-review-receipt.json').write_text(json.dumps({'schema_version':1,
            'workspace_id':self.ctx.workspace_id,'rows':[{'session_id':'canonical-key','source_path':path.relative_to(self.ctx.root).as_posix(),
            'source_bytes':len(raw),'source_sha256':hashlib.sha256(raw).hexdigest(),'reviewed_ranges':[{'from':0,'to':len(raw)}],
            'outcome':'no_change','candidate_ids':[]}]}),'utf-8')

    def test_current_receipt_never_inherits_to_history(self):
        self.receipt();status=manual.status(state_path=self.ctx.state/'manual.json',state_sessions=self.ctx.sessions_state,archive_root=self.ctx.raw_message_store)
        self.assertEqual(['canonical-key'],[r['session_id'] for r in status['reviewed_sessions']])
        history=next(r for r in status['pending_sessions'] if r['session_id']=='hermes:stable')
        self.assertEqual('historical',history['snapshot_kind']);self.assertEqual('historical',history['preview'])

    def test_same_title_without_manifest_alias_is_not_merged(self):
        p=self.new/'manifest.json';m=json.loads(p.read_text('utf-8'));m.pop('source_session_id');p.write_text(json.dumps(m),'utf-8')
        items=self.items();self.assertEqual(2,len(items))
        self.assertNotIn('snapshot_kind',items['canonical-key'])

    def test_source_classification_still_excludes_internal_alias(self):
        p=self.new/'manifest.json';m=json.loads(p.read_text('utf-8'));m['archive_kind']='subtask';p.write_text(json.dumps(m),'utf-8')
        self.assertEqual({},self.items())

    def test_ambiguous_canonical_alias_never_selects_by_order(self):
        self.make('duplicate','another-key',[self.message('new','current')],source='hermes:stable')
        items=self.items()
        for key in ('canonical-key','another-key'):
            self.assertTrue(items[key]['live_snapshot_pending']);self.assertNotIn('live_snapshot_matched',items[key])


if __name__=='__main__':unittest.main()
