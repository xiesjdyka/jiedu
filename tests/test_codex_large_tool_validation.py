import json,unittest,io
from types import SimpleNamespace
from knowledge_bridge.codex_large_records import scan,StreamedInternal
from knowledge_bridge.codex_large_records import new_parser,feed,known_internal,internal_prefix

class LargeToolValidatorTests(unittest.TestCase):
 def test_only_fully_validated_unambiguous_known_tool_envelope_is_internal(self):
  raw=json.dumps({'type':'response_item','payload':{'type':'function_call_output','call_id':'fixture','output':'synthetic '+('中'*1000)}}).encode();state=new_parser()
  for start in range(0,len(raw),17):feed(state,raw[start:start+17],final=start+17>=len(raw))
  self.assertTrue(known_internal(state));self.assertTrue(internal_prefix(raw[:256]))
  for data in [b'{"type":"response_item","payload":{"type":"message","role":"assistant","content":[]}}',b'{"type":"response_item","payload":{"type":"unknown_tool"}}',b'{"type":"response_item","payload":{"type":"function_call_output"},"payload":{"type":"message"}}',b'{"type":"response_item","payload":{"type":"function_call_output","type":"message"}}',b'{"type":"response_item","type":"world_state","payload":{"type":"function_call_output"}}']:
   state=new_parser();feed(state,data,final=True);self.assertFalse(known_internal(state),data)
 def test_malformed_tool_result_never_qualifies_for_cursor_advance(self):
  state=new_parser()
  with self.assertRaises(ValueError):feed(state,b'{"type":"response_item","payload":{"type":"function_call_output","output":"invalid',final=True)
 def test_segment_restart_validates_entire_record_and_unknown_public_never_advances(self):
  source=SimpleNamespace(object_id='synthetic-object',mtime_ns=1)
  for kind in ['function_call_output','message','unknown_tool']:
   raw=json.dumps({'type':'response_item','payload':{'type':kind,'output':'synthetic '*2000}},separators=(',',':')).encode()+b'\n'
   adapter=SimpleNamespace(_work_cursor={},stream_page_bytes=4096);handle=io.BytesIO(raw)
   value,end,pending=scan(adapter,handle,source,0,len(raw));self.assertIsNone(value);self.assertEqual(0,end)
   adapter._work_cursor=json.loads(json.dumps(adapter._work_cursor))
   if kind=='function_call_output':
    for _ in range(20):
     value,end,pending=scan(adapter,handle,source,0,len(raw))
     if value is not None:break
    self.assertIsInstance(value,StreamedInternal);self.assertEqual(len(raw),end)
   else:
    with self.assertRaises(ValueError):
     for _ in range(20):scan(adapter,handle,source,0,len(raw))
    self.assertEqual(0,adapter._work_cursor['large_record']['start'])
 def test_changed_earlier_type_is_rejected_by_final_full_validation(self):
  raw=b'{"type":"response_item","payload":{"type":"function_call_output","output":"'+b'x'*20000+b'"}}\n'
  adapter=SimpleNamespace(_work_cursor={},stream_page_bytes=4096);source=SimpleNamespace(object_id='synthetic-object',mtime_ns=1);handle=io.BytesIO(raw)
  scan(adapter,handle,source,0,len(raw));changed=raw.replace(b'response_item',b'unknown_event');self.assertEqual(len(raw),len(changed));handle=io.BytesIO(changed)
  with self.assertRaises(ValueError):
   for _ in range(20):scan(adapter,handle,source,0,len(raw))
