import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from dateutil.tz import gettz
from knowledge_bridge.wiki_daily_runs import daily_window, iso
from knowledge_bridge.writer_core import save
from tests.test_knowledge_wiki import WikiFixture
from tests import test_wiki_daily_runs as fixture
import sqlite3
from contextlib import closing
from knowledge_bridge.wiki_daily_runs import WikiDailyRuns

RULE={'BYHOUR':'9','BYMINUTE':'0'}

class DailyTimezoneTests(unittest.TestCase):
 def test_explicit_config_overrides_binding_and_legacy_offset(self):
  now=datetime(2026,10,2,17,tzinfo=timezone.utc)
  due,nxt,info=daily_window(now,RULE,{'timezone':'Asia/Shanghai','utc_offset_minutes':480},{'timezone':'America/Los_Angeles'})
  self.assertEqual('2026-10-02T16:00:00+00:00',iso(due));self.assertEqual('scheduler_config',info['timezone_source'])
 def test_host_zone_ignores_stale_offset(self):
  now=datetime(2026,10,2,17,tzinfo=timezone.utc)
  due,nxt,info=daily_window(now,RULE,{'utc_offset_minutes':480},{},gettz('America/Los_Angeles'))
  self.assertEqual('2026-10-02T16:00:00+00:00',iso(due));self.assertEqual('scheduler_host_local',info['timezone_source'])
 def test_dst_next_day_is_not_always_24_hours(self):
  for day,hours in [('2026-03-07T18:00:00+00:00',23),('2026-10-31T17:00:00+00:00',25)]:
   due,nxt,_=daily_window(datetime.fromisoformat(day),RULE,{}, {'timezone':'America/Los_Angeles'})
   self.assertEqual(hours,(nxt-due).total_seconds()/3600)
 def test_unknown_explicit_zone_does_not_guess(self):
  with self.assertRaises(ValueError):daily_window(datetime.now(timezone.utc),RULE,{}, {'timezone':'Unknown/Invalid'})

class DailyReceiptTimezoneTests(WikiFixture):
 def setUp(self):
  super().setUp();self.clock=datetime(2026,9,27,1,5,tzinfo=timezone.utc);self.daily=WikiDailyRuns(self.ctx,lambda:self.clock)
  self.db=self.ctx.control/'test-history.sqlite';self.config=self.ctx.control/'scheduler.toml'
  self.config.write_text('id="daily"\nstatus="ACTIVE"\ntarget_thread_id="runner"\nrrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0"\n','utf-8')
  self.binding={'workspace_id':self.ctx.workspace_id,'identities':[{'source_key':'codex','source_session_id':'runner'}],'scheduler':{'id':'daily','config_path':str(self.config),'history_db':str(self.db),'client_id':'client','integrated_at':'2026-09-26T09:00:00+00:00','utc_offset_minutes':480,'receipt_grace_seconds':1800}}
  save(self.ctx.state/'organization-runner.json',self.binding)
  with closing(sqlite3.connect(self.db)) as db, db:db.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER,first_user_item_id TEXT);CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT);')
 heartbeat=fixture.WikiDailyTests.heartbeat
 enable=fixture.WikiDailyTests.enable
 start=fixture.WikiDailyTests.start
 finish=fixture.WikiDailyTests.finish
 def pacific(self):
  self.clock=datetime(2026,10,2,16,5,tzinfo=timezone.utc)
  self.binding['scheduler'].pop('timezone',None);save(self.ctx.state/'organization-runner.json',self.binding)
  from knowledge_bridge import wiki_daily_runs
  original=wiki_daily_runs.daily_window
  self.patch=patch.object(wiki_daily_runs,'daily_window',side_effect=lambda now,rule,schedule,config:original(now,rule,schedule,config,gettz('America/Los_Angeles')))
  self.patch.start();self.addCleanup(self.patch.stop)
 def test_completed_pacific_run_stays_succeeded_with_old_offset(self):
  self.pacific();self.enable();self.heartbeat(hour=16);run=self.start();self.complete(run['job']);self.finish(run)
  self.clock=self.clock.replace(hour=23)
  result=self.daily.status();self.assertEqual('succeeded',result['execution_state']);self.assertIsNotNone(result['last_success_at']);self.assertEqual('scheduler_host_local',result['timezone_source'])
 def test_genuinely_missing_pacific_run_reports_missing_receipt(self):
  self.pacific();self.enable();self.clock=self.clock.replace(hour=17)
  result=self.daily.status();self.assertEqual('missing_receipt',result['execution_state']);self.assertEqual('2026-10-02T16:00:00+00:00',result['expected_at'])

if __name__=='__main__':unittest.main()
