import json,sqlite3,unittest,os
from contextlib import closing
from datetime import datetime,timedelta,timezone
from tests.test_knowledge_wiki import WikiFixture
from knowledge_bridge.wiki_daily_runs import WikiDailyRuns,iso
from knowledge_bridge.writer_core import save

class WikiDailyTests(WikiFixture):
 def setUp(self):
  super().setUp();self.clock=datetime(2026,9,27,1,5,tzinfo=timezone.utc);self.daily=WikiDailyRuns(self.ctx,lambda:self.clock)
  self.db=self.ctx.control/'test-history.sqlite';self.config=self.ctx.control/'scheduler.toml'
  self.config.write_text('id="daily"\nstatus="ACTIVE"\ntarget_thread_id="runner"\nrrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0"\n','utf-8')
  self.binding={'workspace_id':self.ctx.workspace_id,'identities':[{'source_key':'codex','source_session_id':'runner'}],'scheduler':{'id':'daily','config_path':str(self.config),'history_db':str(self.db),'client_id':'client','integrated_at':'2026-09-26T09:00:00+00:00','utc_offset_minutes':480,'receipt_grace_seconds':1800}}
  self.binding['scheduler']['timezone']='Asia/Shanghai'
  save(self.ctx.state/'organization-runner.json',self.binding)
  with closing(sqlite3.connect(self.db)) as db, db:
   db.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER,first_user_item_id TEXT);CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT);')
 def heartbeat(self,turn='hb1',thread='runner',automation='daily',status='inProgress',hour=1,ordinary=False):
  stamp=self.clock.replace(hour=hour,minute=0,second=0)
  text='ordinary completed work' if ordinary else '<heartbeat>\n<automation_id>'+automation+'</automation_id>\n<current_time_iso>'+iso(stamp)+'</current_time_iso>\n<instructions>Fixture</instructions>\n</heartbeat>'
  with closing(sqlite3.connect(self.db)) as db, db:
   db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',(thread,turn,status,int(stamp.timestamp()),int(stamp.timestamp())+60 if status=='completed' else None,turn+'-user'))
   db.execute('INSERT INTO thread_items VALUES(?,?,?,?)',(thread,turn,turn+'-user',json.dumps({'type':'userMessage','content':[{'type':'text','text':text}]})))
 def enable(self):
  state=self.wiki.load();state.update(enabled=True,enabled_since='2026-09-26T09:00:00+00:00');save(self.wiki.path,state)
 def start(self):return self.daily.start('client',lambda _:None)
 def finish(self,run,outcome='succeeded',reason=''):return self.daily.finish('client',lambda _:None,run['run']['run_id'],outcome,reason)
 def test_arranged_not_run_and_ordinary_completion_not_wiki_success(self):
  self.enable();self.heartbeat(ordinary=True,status='completed')
  result=self.daily.status();self.assertEqual('arranged',result['arrangement']);self.assertEqual('not_run',result['execution_state']);self.assertIsNone(result['last_success_at'])
 def test_disabled_does_not_enable_or_create_job(self):
  self.heartbeat();result=self.start();self.assertEqual('disabled',result['status']);self.assertFalse(self.wiki.load()['enabled']);self.assertFalse(self.daily.path.exists())
 def test_verified_heartbeat_actual_claim_and_success(self):
  self.enable();self.heartbeat();run=self.start();self.assertEqual('hb1',run['run']['heartbeat_turn_id']);self.assertEqual(run['run']['run_id'],self.start()['run']['run_id'])
  self.assertEqual([run['job']['job_id']],run['run']['job_ids']);self.complete(run['job']);result=self.finish(run)
  self.assertEqual('succeeded',result['status']);status=self.daily.status();self.assertEqual('succeeded',status['execution_state']);self.assertEqual(iso(self.clock),status['last_success_at'])
  self.assertTrue(self.finish(run)['duplicate'])
 def test_forged_wrong_task_and_wrong_client_rejected(self):
  self.enable();self.heartbeat(thread='other');self.heartbeat(turn='wrong',automation='other')
  with self.assertRaises(ValueError):self.start()
  self.heartbeat(turn='valid')
  with self.assertRaises(ValueError):self.daily.start('client',lambda _:None,'invented-turn')
  with self.assertRaises(PermissionError):self.daily.start('other-client',lambda _:None,'valid')
  self.assertFalse(self.daily.path.exists())
 def test_unread_job_cannot_finish_or_substitute_ordinary_job(self):
  self.enable();self.heartbeat();run=self.start()
  with self.assertRaisesRegex(ValueError,'尚未读完'):self.finish(run)
  self.assertIsNone(self.daily.status()['last_success_at'])
  with self.assertRaises(ValueError):self.daily.finish('client',lambda _:None,'ordinary-org-job','succeeded')
 def test_finished_old_heartbeat_cannot_be_backfilled_as_wiki(self):
  self.enable();self.heartbeat(status='completed')
  with self.assertRaisesRegex(ValueError,'旧普通整理'):self.start()
  self.assertFalse(self.daily.path.exists())
 def test_reported_failure_keeps_jobs_and_reason(self):
  self.enable();self.heartbeat();run=self.start();self.finish(run,'failed','AI服务不可用')
  status=self.daily.status();self.assertEqual('failed',status['execution_state']);self.assertEqual('AI服务不可用',status['failure']);self.assertTrue(self.wiki.organizer.path(run['job']['job_id']).exists());self.assertIsNone(status['last_success_at'])
 def test_missing_start_receipt_after_due(self):
  self.enable();self.heartbeat(status='completed');self.clock+=timedelta(hours=1)
  status=self.daily.status();self.assertEqual('missing_receipt',status['execution_state']);self.assertIsNone(status['last_started_at']);self.assertIsNone(status['last_success_at'])
 def test_started_run_without_completion_expires(self):
  self.enable();self.heartbeat();run=self.start();os.utime(self.wiki.organizer.path(run['job']['job_id']),(self.clock.timestamp(),self.clock.timestamp()));self.clock+=timedelta(hours=1)
  status=self.daily.status();self.assertEqual('missing_receipt',status['execution_state']);self.assertIn('已开始',status['failure'])
 def test_provider_failure_is_not_success(self):
  self.enable();self.heartbeat();self.start()
  with closing(sqlite3.connect(self.db)) as db, db:db.execute("UPDATE thread_turns SET status='failed'")
  self.assertEqual('failed',self.daily.status()['execution_state']);self.assertIsNone(self.daily.status()['last_success_at'])
 def test_missing_schema_does_not_break_manual_wiki(self):
  self.enable();self.db.unlink();self.assertEqual('unverified',self.daily.status()['execution_state']);self.assertEqual('claimed',self.wiki.claim('non-codex',lambda _:None)['status'])
 def test_increment_after_read_is_deferred_until_next_batch(self):
  self.enable();self.heartbeat();run=self.start();self.complete(run['job']);self.file.write_text(self.file.read_text('utf-8')+'增量\n','utf-8')
  done=self.finish(run);self.assertEqual('succeeded',done['status']);self.assertEqual(1,len(done['run']['job_ids']));self.assertEqual('server_verified_fixed_snapshot_batch',done['run']['completion_basis']);next_job=self.wiki.claim('client',lambda _:None,new_batch=True);self.assertEqual(1,next_job['total'])
 def test_pause_prevents_daily_success_and_does_not_reenable(self):
  self.enable();self.heartbeat();run=self.start();self.complete(run['job']);self.wiki.pause()
  self.assertEqual('paused',self.finish(run)['status']);self.assertFalse(self.wiki.load()['enabled']);self.assertIsNone(self.daily.status()['last_success_at'])

 def empty_turn(self,turn,status='inProgress'):
  with closing(sqlite3.connect(self.db)) as db,db:
   db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',('runner',turn,status,int(self.clock.timestamp()),None,None))

 def test_empty_retry_turns_cannot_relabel_interrupted_heartbeat(self):
  self.enable();self.heartbeat(status='interrupted');self.empty_turn('retry-one','interrupted');self.empty_turn('retry-two','completed')
  with self.assertRaisesRegex(ValueError,'手动接续'):self.start()
  for turn in ('retry-one','retry-two'):
   with self.assertRaisesRegex(ValueError,'真实心跳'):self.daily.start('client',lambda _:None,turn)
  status=self.daily.status();self.assertEqual('interrupted',status['execution_state'])
  self.assertIsNone(status['last_started_at']);self.assertIsNone(status['last_success_at'])
  self.assertFalse(status['recovery']['counts_as_scheduled_success']);self.assertFalse(self.daily.path.exists())

 def test_new_interruption_keeps_yesterday_failure_but_shows_today(self):
  self.enable();self.heartbeat();run=self.start();self.finish(run,'failed','yesterday failure')
  original=self.daily.path.read_bytes();self.clock+=timedelta(days=1)
  self.heartbeat('today',status='interrupted');self.empty_turn('retry')
  status=self.daily.status();self.assertEqual('interrupted',status['execution_state'])
  self.assertEqual('today',status['current_heartbeat']['heartbeat_turn_id'])
  self.assertEqual('yesterday failure',status['previous_run']['failure'])
  self.assertEqual(original,self.daily.path.read_bytes())

 def test_manual_recovery_resumes_cursor_without_daily_receipt(self):
  self.enable();job=self.claim();source=job['items'][0]['source_path']
  page=self.wiki.organizer.read('client',job['job_id'],source,0,8)
  self.heartbeat(status='interrupted');self.empty_turn('manual')
  with self.assertRaises(ValueError):self.start()
  resumed=self.wiki.claim('client',lambda _:None,require_enabled=True)
  self.assertEqual(job['job_id'],resumed['job_id']);self.assertEqual(page['resume_offset'],resumed['items'][0]['offset'])
  self.complete(resumed);self.assertFalse(self.daily.path.exists());self.assertIsNone(self.daily.status()['last_success_at'])

 def test_ended_started_run_cannot_be_completed_by_manual_work(self):
  self.enable();self.heartbeat();run=self.start()
  with closing(sqlite3.connect(self.db)) as db,db:db.execute("UPDATE thread_turns SET status='completed',completed_at=?",(int(self.clock.timestamp()),))
  original=self.daily.path.read_bytes();self.complete(run['job'])
  with self.assertRaisesRegex(ValueError,'手动整理'):self.finish(run)
  with self.assertRaisesRegex(ValueError,'已结束'):self.start()
  self.assertEqual(original,self.daily.path.read_bytes());self.assertIsNone(self.daily.status()['last_success_at'])

 def test_manual_recovery_guidance_does_not_enable_paused_wiki(self):
  self.enable();self.heartbeat(status='interrupted');self.wiki.pause()
  before=self.wiki.path.read_bytes();status=self.daily.status()
  self.assertEqual('disabled',status['execution_state']);self.assertFalse(status.get('recovery',{}).get('available',False))
  self.assertEqual(before,self.wiki.path.read_bytes())

if __name__=='__main__':unittest.main()
