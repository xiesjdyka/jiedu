import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_bridge.workspace import initialize_workspace
from knowledge_bridge.organization_review import load_receipt,review_state
from knowledge_bridge import manual_extraction as manual
from knowledge_bridge.source_adapters import legacy_client_view


class ReviewDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ctx=initialize_workspace(Path(self.tmp.name)/'vault')
        self.file=self.ctx.raw_archive/'Hermes'/'example'/'conversation.md'
        self.file.parent.mkdir(parents=True);self.file.write_bytes(b'# source\n\nuser decision\n')
        self.item={'archive_path':str(self.file.parent),'client_id':'hermes','canonical_messages':[{'message_id':'m1','role':'user','content':'text'}]}
        raw=self.file.read_bytes()
        self.row={'session_id':'s','source_path':self.file.relative_to(self.ctx.root).as_posix(),
                  'source_sha256':hashlib.sha256(raw).hexdigest(),'source_bytes':len(raw),
                  'reviewed_ranges':[{'from':0,'to':len(raw)}],'outcome':'no_change','candidate_ids':[]}

    def state(self,row=None,lookup=lambda cid:{'status':'written'}):
        return review_state(self.ctx,self.item,row or self.row,lookup)

    def receipt(self,workspace=None):
        path=self.ctx.state/'organization-review-receipt.json'
        path.write_text(json.dumps({'schema_version':1,'workspace_id':workspace or self.ctx.workspace_id,'rows':[self.row]}),'utf-8')
        return path

    def test_current_complete_read_is_no_change_but_growth_requires_reading(self):
        self.assertEqual('no_change',self.state()['review_state'])
        self.file.write_bytes(self.file.read_bytes()+b'new message\n')
        self.assertEqual('changed',self.state()['review_state'])

    def test_partial_and_gapped_ranges_are_not_complete(self):
        for ranges in ([{'from':1,'to':self.row['source_bytes']}],[{'from':0,'to':5},{'from':7,'to':self.row['source_bytes']}],[]):
            self.assertEqual('partial',self.state(self.row|{'reviewed_ranges':ranges})['review_state'])

    def test_written_claim_must_match_candidate_status(self):
        row=self.row|{'outcome':'written','candidate_ids':['c']}
        self.assertEqual('organized',self.state(row)['review_state'])
        self.assertEqual('reviewed',self.state(row,lambda cid:{'status':'pending'})['review_state'])
        self.assertEqual('unreviewed',self.state(row,lambda cid:(_ for _ in ()).throw(KeyError(cid)))['review_state'])

    def test_receipt_cannot_cross_workspace_or_session(self):
        self.receipt('other');self.assertFalse(load_receipt(self.ctx)[0])
        with patch.object(Path,'read_bytes',side_effect=AssertionError('must not read unrelated path')):
            row=self.row|{'source_path':'../outside.md'}
            self.assertEqual('unreviewed',self.state(row)['review_state'])
        self.item['archive_path']=str(self.ctx.raw_archive/'another')
        self.assertEqual('unreviewed',self.state()['review_state'])

    def test_display_does_not_change_source_or_receipt_and_only_explicit_defer_counts(self):
        receipt=self.receipt();before=(receipt.read_bytes(),self.file.read_bytes())
        state=Path(self.tmp.name)/'manual.json'
        with patch.object(manual,'active_workspace',return_value=self.ctx),patch.object(manual,'_sessions',return_value={'s':self.item}),patch.object(manual,'_session_rows',return_value=self.item['canonical_messages']):
            result=manual.status(state_path=state)
            self.assertEqual(1,result['reviewed_session_count']);self.assertEqual(0,result['deferred_session_count'])
            state.write_text(json.dumps({'session_states':{'s':'deferred'}}),'utf-8')
            result=manual.status(state_path=state)
            self.assertEqual(1,result['deferred_session_count']);self.assertEqual(0,result['reviewed_session_count'])
        self.assertEqual(before,(receipt.read_bytes(),self.file.read_bytes()))

    def test_hermes_without_receipt_is_unreviewed_not_user_deferred(self):
        with patch.object(manual,'active_workspace',return_value=self.ctx),patch.object(manual,'_sessions',return_value={'s':self.item}),patch.object(manual,'_session_rows',return_value=self.item['canonical_messages']):
            result=manual.status(state_path=Path(self.tmp.name)/'empty.json')
        self.assertEqual(1,result['pending_session_count']);self.assertEqual(0,result['deferred_session_count'])

    def test_legacy_entry_has_no_new_identity_and_does_not_inherit_failed_read(self):
        rows=[{'adapter_id':'hermes_local','saved':{'message_count':4}}]
        entry=legacy_client_view({'id':'hermes','client':'Hermes','configured':True,'client_access':True,
            'tool_permissions':{'search_memory':True,'modify_profile':False},
            'recent_mcp_calls':[{'tool':'knowledge_search','outcome':'success','at':'old'},
                                {'tool':'knowledge_search','outcome':'rejected','at':'new'}]},rows)
        self.assertEqual('legacy_unverified',entry['status']);self.assertNotIn('internal_id',entry)
        self.assertEqual('old',entry['last_successful_read']['at']);self.assertEqual('deny',entry['protected_access'])

    def test_explicit_hermes_selection_works_without_expanding_implicit_export(self):
        with patch.object(manual,'_sessions',return_value={'s':self.item}),patch.object(manual,'_session_rows',return_value=self.item['canonical_messages']):
            args={'state_path':Path(self.tmp.name)/'manual.json','packages_dir':Path(self.tmp.name)/'packages'}
            with self.assertRaises(ValueError):manual.export_package(**args)
            result=manual.export_package(session_ids=['s'],**args)
            self.assertEqual(['s'],result['session_ids'])

    def test_duplicate_receipt_identity_cannot_choose_arbitrary_result(self):
        path=self.receipt();value=json.loads(path.read_text('utf-8'));value['rows'].append(dict(self.row));path.write_text(json.dumps(value),'utf-8')
        self.assertIsNone(load_receipt(self.ctx)[0]['s'])

if __name__=='__main__':unittest.main()
