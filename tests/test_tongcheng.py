import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from tongcheng import TongchengService
from contracts import OutboundMessageCommand
from qianniu_knowledge import FAQ

class Transport:
 def __init__(self):
  self.s={'logged_in':True,'identity_verified':True,'identity_source':'platform_dom','conversation_id':'c1','input_text':'','messages':[]}
  self.calls=0
  self.echo='out1'
 async def snapshot(self):return self.s
 async def send_checked(self,*args):
  self.calls+=1
  return self.echo

class Tests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.sqlite';self.t=Transport()
  now=datetime.now(timezone.utc)
  self.faq=FAQ(id='f1',question='您好',answer='您好，请问有什么可以帮您？',valid_from=now-timedelta(days=1),expires_at=now+timedelta(days=1),reviewed=True,enabled=True,version=1)
  self.s=TongchengService(self.path,self.t,lambda:[self.faq]);self.s.active=True
 def tearDown(self):self.s.db.close();self.temp.cleanup()
 def msg(self,id='m1',direction='inbound',text='您好'):
  return {'id':id,'direction':direction,'text':text,'kind':'text'}
 def ingest(self):return self.s.ingest('a',self.t.s)
 def task(self):return self.s.db.execute('select * from tasks').fetchone()
 def prepare(self):
  self.ingest();self.t.s['messages']=[self.msg()];self.ingest();t=self.task()
  return OutboundMessageCommand('a','c1',{'type':'text','text':t['text']},t['id'])
 async def test_baseline_duplicate_and_same_text_new_id(self):
  self.t.s['messages']=[self.msg()];self.assertEqual(self.ingest(),[]);self.assertIsNone(self.task())
  self.t.s['messages'].append(self.msg('m2'));self.assertEqual(len(self.ingest()),1);self.assertEqual(self.ingest(),[])
  self.assertEqual(self.s.db.execute("SELECT count(*) FROM audit WHERE action='customer_message_received'").fetchone()[0],1)

 def test_audit_metadata_does_not_accept_credentials_or_payloads(self):
  import json
  self.s.audit('test','task',status='not_accepted',duration_ms=123,api_key='synthetic-secret',access_token='synthetic-token',payload='private-payload')
  row=self.s.db.execute('SELECT details FROM audit ORDER BY id DESC LIMIT 1').fetchone()[0]
  self.assertEqual(json.loads(row),{'status':'not_accepted','duration_ms':123})
  self.assertNotIn('synthetic',row);self.assertNotIn('private-payload',row)
 async def test_echo_and_no_second_send(self):
  c=self.prepare();self.assertTrue((await self.s.send_message(c)).accepted)
  self.assertFalse((await self.s.send_message(c)).accepted);self.assertEqual(self.t.calls,1)

 async def test_shadow_blocks_manual_and_model_sends_and_survives_restart(self):
  c=self.prepare();self.s.policy('a','shadow')
  self.assertEqual((await self.s.send_message(c)).error,'shadow_mode_no_send')
  self.s.edit_task(c.idempotency_key,'人工编辑测试')
  c=OutboundMessageCommand('a','c1',{'type':'text','text':'人工编辑测试'},c.idempotency_key)
  self.assertEqual((await self.s.send_message(c)).error,'shadow_mode_no_send')
  self.s.record_simulation(c.idempotency_key,'no_match')
  self.assertEqual(self.s.overview()['simulations'][0]['text'],'')
  self.assertEqual(self.t.calls,0)
  self.s.db.close();self.s=TongchengService(self.path,self.t,lambda:[self.faq]);self.s.active=True
  self.assertEqual((await self.s.send_message(c)).error,'shadow_mode_no_send')
 async def test_shadow_receives_first_live_wire_message_without_server_time(self):
  self.s.policy('a','shadow')
  self.t.s.update(identity_source='platform_websocket',conversation_id='ws:customer',message_id_source='platform_websocket_fingerprint',conversation_reference_source='platform_websocket_uid')
  self.t.s['messages']=[dict(self.msg(),newly_received=True)]
  self.assertEqual(len(self.ingest()),1)
  self.assertEqual(self.ingest(),[])
  self.assertEqual(self.task()['conversation'],'ws:customer')
 async def test_changed_target_and_draft_block(self):
  c=self.prepare();self.t.s['conversation_id']='c2';self.assertFalse((await self.s.send_message(c)).accepted)
  self.t.s['conversation_id']='c1';self.t.s['input_text']='人工草稿';self.assertFalse((await self.s.send_message(c)).accepted);self.assertEqual(self.t.calls,0)
 async def test_human_persists(self):
  c=self.prepare();self.s.handoff('a','c1',True);self.assertFalse((await self.s.send_message(c)).accepted)
  self.s.db.close();self.s=TongchengService(self.path,self.t);self.assertTrue(self.s.is_human('a','c1'))
 async def test_uncertain_not_retried(self):
  c=self.prepare();self.t.echo=None;await self.s.send_message(c);await self.s.send_message(c)
  self.assertEqual(self.task()['status'],'uncertain');self.assertEqual(self.t.calls,1)
 async def test_restart_baseline(self):
  self.prepare();self.s.db.close();self.s=TongchengService(self.path,self.t)
  self.t.s['messages'].append(self.msg('m2'));self.assertEqual(self.ingest(),[])
  self.assertEqual(self.s.db.execute('select count(*) from tasks').fetchone()[0],1)
 async def test_outbound_handoff_and_unsupported(self):
  self.ingest();self.t.s['messages']=[self.msg('o','outbound')];self.ingest();self.assertTrue(self.s.is_human('a','c1'))
 async def test_unverified_identity_rejected(self):
  self.t.s['identity_source']='nickname'
  with self.assertRaises(ValueError):self.ingest()
 async def test_crash_claim_becomes_uncertain(self):
  self.prepare();self.s.db.execute("update tasks set status='sending'");self.s.db.commit();self.s.db.close();self.s=TongchengService(self.path,self.t)
  self.assertEqual(self.task()['status'],'uncertain')

 async def test_manual_reply_does_not_require_faq(self):
  command=self.prepare();self.s.knowledge=lambda:[]
  self.s.edit_task(command.idempotency_key,'人工核对后的回复')
  edited=OutboundMessageCommand('a','c1',{'type':'text','text':'人工核对后的回复'},command.idempotency_key)
  self.assertTrue((await self.s.send_message(edited)).accepted)

 async def test_return_to_ai_cannot_reactivate_old_draft(self):
  command=self.prepare();self.s.handoff('a','c1',True);self.s.handoff('a','c1',False)
  self.assertEqual(self.task()['status'],'cancelled')
  self.assertFalse((await self.s.send_message(command)).accepted)

 async def test_new_message_cancels_old_task(self):
  command=self.prepare();self.t.s['messages'].append(self.msg('m2'));self.ingest()
  self.assertFalse((await self.s.send_message(command)).accepted)
  self.assertEqual(self.t.calls,0)

 async def test_model_draft_uses_shared_reviewed_fragments(self):
  from workbench_core.knowledge import ModelConfig
  command=self.prepare()
  config=ModelConfig(enabled=True,endpoint='https://example.com/chat/completions',model='test',api_key='synthetic-test-key')
  result=await self.s.generate_task(command.idempotency_key,config,provider=lambda request:'{"references":[{"faq_id":"f1","fragment_index":0}]}')
  self.assertEqual(result['status'],'draft_ready')
  self.assertEqual(self.task()['source'],'model')
  self.assertTrue((await self.s.send_message(command)).accepted)

 async def test_auto_requires_prior_manual_echo(self):
  with self.assertRaisesRegex(ValueError,'verified_manual_echo'):self.s.policy('a','test_auto',['c1'])
  command=self.prepare();await self.s.send_message(command)
  self.s.policy('a','test_auto',['c1'])
  self.s.policy('a','readonly')

 async def test_new_customer_after_initial_baseline_is_not_silenced(self):
  self.ingest();self.s.initial_scan_done=True
  self.t.s['conversation_id']='new-customer'
  self.t.s['messages']=[{**self.msg('new-message'),'occurred_at':(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()}]
  self.assertEqual(len(self.ingest()),1)
  self.assertIsNotNone(self.task())

 async def test_newly_loaded_old_customer_does_not_replay_history(self):
  self.ingest();self.s.initial_scan_done=True
  self.t.s['conversation_id']='old-customer'
  self.t.s['messages']=[{**self.msg('historical'),'occurred_at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}]
  self.assertEqual(self.ingest(),[]);self.assertIsNone(self.task())

 async def test_unclassified_system_message_is_preserved_without_reply(self):
  self.ingest();self.t.s['messages']=[self.msg('system','unknown','')]
  self.assertEqual(self.ingest(),[]);self.assertIsNone(self.task())
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)

 async def test_manual_draft_rejects_unreviewed_new_tail(self):
  self.prepare();self.t.s['messages'].append(self.msg('new-tail'));self.ingest()
  with self.assertRaisesRegex(ValueError,'conversation_changed'):
   self.s.manual_reply('a','c1','针对旧消息的回复',expected_message='m1')
  task=self.s.manual_reply('a','c1','已核对新消息',expected_message='new-tail')
  self.assertEqual(self.s.db.execute('SELECT message FROM tasks WHERE id=?',(task,)).fetchone()[0],'new-tail')

 async def test_product_knowledge_does_not_cross_routes(self):
  self.s.knowledge_scopes=lambda:{'f1':['route-a']}
  self.t.s['products']=[{'external_product_id':'route-b','title':'另一条线路'}]
  self.prepare()
  self.assertEqual(self.s.knowledge_for('a','c1'),[])
  self.assertEqual(self.task()['status'],'human_review')
  self.t.s['products']=[{'external_product_id':'route-a','title':'已审核线路'}];self.ingest()
  self.assertEqual(len(self.s.knowledge_for('a','c1')),1)

 async def test_login_loss_disarms_auto_instead_of_silent_resume(self):
  command=self.prepare();await self.s.send_message(command)
  self.s.policy('a','test_auto',['c1']);self.t.s['logged_in']=False
  await self.s.health('a')
  self.assertEqual(self.s.db.execute('SELECT mode FROM policies WHERE account=?',('a',)).fetchone()[0],'draft')

 async def test_document_draft_stays_in_shadow_results_not_send_tasks(self):
  import json
  from tests.test_shadow_documents import document
  from workbench_core.knowledge import ModelConfig
  self.ingest();self.t.s['messages']=[self.msg(text='产品1888072的行程')];self.ingest()
  task=self.task()['id'];self.s.policy('a','shadow');self.s.shadow_documents=lambda:[document()]
  config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
  provider=lambda _:json.dumps({'answer':'上海迪士尼三天行程。','references':[{'id':'doc','quote':'上海迪士尼三天行程'}],'needs_clarification':False})
  result=await self.s.generate_task(task,config,provider=provider)
  self.assertEqual(result['status'],'shadow_draft_ready')
  self.assertEqual(self.task()['text'],'')
  self.assertEqual(self.task()['status'],'human_review')
  self.assertEqual(self.s.overview()['simulations'][0]['text'],'上海迪士尼三天行程。')
  self.assertFalse(json.loads(self.s.overview()['simulations'][0]['evidence'])[0]['reviewed'])
  self.assertEqual(self.t.calls,0)
  self.s.policy('a','draft')
  from unittest.mock import patch
  with patch('workbench_core.shadow_documents.shadow_draft') as shadow:
   await self.s.generate_task(task,ModelConfig())
   shadow.assert_not_called()
  self.assertEqual(self.task()['text'],'')

 async def test_shadow_can_analyze_new_question_while_human_replies_without_taking_ownership(self):
  import json
  from tests.test_shadow_documents import document
  from workbench_core.knowledge import ModelConfig
  self.ingest();self.s.policy('a','shadow');self.s.handoff('a','c1',True)
  self.t.s['messages']=[self.msg(text='1888072行程')];self.ingest()
  task=self.task()['id'];self.s.shadow_documents=lambda:[document()]
  self.t.s['messages'].append(self.msg('human-reply','outbound','人工正在接待'));self.ingest()
  seen=[]
  def provider(payload):
   seen.append(payload)
   return json.dumps({'answer':'上海迪士尼三天行程。','references':[{'id':'doc','cell':'A2'}],'needs_clarification':False})
  config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
  self.assertEqual((await self.s.generate_task(task,config,provider))['status'],'shadow_draft_ready')
  self.assertTrue(self.s.is_human('a','c1'));self.assertEqual(self.t.calls,0)
  self.assertNotIn('人工正在接待',seen[0]['messages'][-1]['content'])
  self.t.s['messages'].append(self.msg('next-question',text='新的问题'));self.ingest()
  with self.assertRaisesRegex(ValueError,'task_not_editable'):await self.s.generate_task(task,config,provider)

 async def test_product_card_is_context_only_and_next_question_uses_its_knowledge(self):
  import json
  from tests.test_shadow_documents import document
  from workbench_core.knowledge import ModelConfig
  self.ingest();self.s.policy('a','shadow')
  products=[{'external_product_id':'1888072','title':'上海迪士尼三天行程','source':'platform_product_card'}]
  self.t.s['products']=products
  self.t.s['messages']=[{**self.msg('card',text='商品卡'),'kind':'card','products':products}]
  self.assertEqual(self.ingest(),[]);self.assertIsNone(self.task())
  self.t.s['messages'].append(self.msg('question',text='几天的行程？'));self.ingest()
  wrong=document('other');wrong['cells'][1]['value']='9999999'
  self.s.shadow_documents=lambda:[document(),wrong]
  def provider(payload):
   data=json.loads(payload['messages'][-1]['content'])
   self.assertEqual(data['product_hint'][0]['external_product_id'],'1888072')
   self.assertEqual([s['id'] for s in data['sources']],['doc'])
   self.assertEqual(data['question'],'几天的行程？')
   return json.dumps({'answer':'三天行程。','references':[{'id':'doc','cell':'A2'}],'needs_clarification':False})
  config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
  self.assertEqual((await self.s.generate_task(self.task()['id'],config,provider))['status'],'shadow_draft_ready')
  self.assertEqual(self.t.calls,0)

 async def test_shadow_question_followed_by_card_keeps_question_and_requeues(self):
  import json
  from tests.test_shadow_documents import document
  from workbench_core.knowledge import ModelConfig
  self.ingest();self.s.policy('a','shadow')
  question=dict(self.msg('q',text='介绍下这个行程'),received_at='2026-10-27T10:16:07.050273+00:00')
  self.t.s['messages']=[question];self.ingest();task=self.task()['id']
  products=[{'external_product_id':'1888072','title':'上海迪士尼三天行程'}]
  card=dict(self.msg('card',text='商品卡'),kind='card',products=products,received_at='2026-10-27T10:16:07.079232+00:00')
  self.t.s['products']=products;self.t.s['messages'].append(card)
  events=self.ingest()
  self.assertEqual([e.external_message_id for e in events],['q'])
  self.assertEqual(self.task()['status'],'human_review')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
  self.s.shadow_documents=lambda:[document()]
  def provider(payload):
   data=json.loads(payload['messages'][-1]['content'])
   self.assertEqual(data['product_hint'],products)
   return json.dumps({'answer':'三天行程。','references':[{'id':'doc','cell':'A2'}],'needs_clarification':False})
  config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
  self.assertEqual((await self.s.generate_task(task,config,provider))['status'],'shadow_draft_ready')
  self.assertEqual(self.t.calls,0)
  self.assertEqual(self.ingest(),[])
  self.t.s['messages'].append(dict(card,id='late',received_at='2026-10-27T10:17:00+00:00'))
  self.ingest();self.assertEqual(self.task()['status'],'cancelled')

if __name__=='__main__':unittest.main()
