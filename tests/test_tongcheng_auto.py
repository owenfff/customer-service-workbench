import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from tests.test_tongcheng import Transport
from tests.test_shadow_documents import document
from tongcheng import TongchengService
from contracts import OutboundMessageCommand
from workbench_core.knowledge import ModelConfig
from tongcheng_model import provider, status
import httpx


class AutoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.transport=Transport()
        self.service=TongchengService(Path(self.temp.name)/'state.sqlite',self.transport)
        self.service.active=True;self.doc=document();self.service.shadow_documents=lambda:[self.doc]
        self.service.model_available=lambda:True
        self.service.policy('a','draft')
        self.service.ingest('a',self.transport.s)
        self.transport.s['messages']=[{'id':'m1','direction':'inbound','text':'1888072行程','kind':'text'}]
        self.service.ingest('a',self.transport.s)
        self.task=self.service.db.execute('SELECT * FROM tasks').fetchone()
        self.config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
        self.expiry=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()

    def tearDown(self):
        self.service.db.close();self.temp.cleanup()

    def approve(self):self.service.review_document('doc',True,self.expiry)

    async def generate(self):
        answer={'status':'shadow_draft_ready','text':'上海迪士尼三天行程。','evidence':[{'document_id':'doc','source_sha256':'version','quote':'上海迪士尼三天行程'}]}
        with patch('workbench_core.shadow_documents.document_draft',return_value=answer):
            return await self.service.generate_task(self.task['id'],self.config)

    async def send(self):
        row=self.service.db.execute('SELECT * FROM tasks WHERE id=?',(self.task['id'],)).fetchone()
        return await self.service.send_message(OutboundMessageCommand('a','c1',{'type':'text','text':row['text']},row['id']))

    async def test_approved_generation_send_and_no_duplicate(self):
        self.approve();self.assertEqual((await self.generate())['status'],'draft_ready')
        self.assertTrue((await self.send()).accepted)
        self.assertFalse((await self.send()).accepted);self.assertEqual(self.transport.calls,1)

    async def test_auto_revoked_authority_blocks_even_valid_document_reply(self):
        self.approve();await self.generate()
        self.service.db.execute("UPDATE policies SET mode='auto'");self.service.db.commit()
        self.service.production_authorized=lambda:False
        self.assertEqual((await self.send()).error,'production_authorization_invalid')
        self.assertEqual(self.transport.calls,0)

    async def test_review_revision_expiry_and_human_takeover_block_send(self):
        self.approve();await self.generate()
        self.doc['content_hash']='edited'
        self.assertFalse((await self.send()).accepted);self.assertEqual(self.transport.calls,0)
        self.assertEqual(self.service.approved_documents(),[])
        self.doc['content_hash']='hash';self.service.handoff('a','c1',True)
        self.assertFalse((await self.send()).accepted)

    async def test_new_question_and_model_outage_block_old_reply(self):
        self.approve();await self.generate();self.service.model_available=lambda:False
        self.assertFalse((await self.send()).accepted)
        self.service.model_available=lambda:True
        self.transport.s['messages'].append({'id':'m2','direction':'inbound','text':'新的问题','kind':'text'})
        self.service.ingest('a',self.transport.s)
        self.assertFalse((await self.send()).accepted);self.assertEqual(self.transport.calls,0)

    async def test_auto_requires_real_acceptance_and_discards_backlog(self):
        self.approve()
        with self.assertRaisesRegex(ValueError,'real_send_acceptance_required'):self.service.policy('a','auto')
        self.service.db.execute("UPDATE tasks SET status='customer_confirmed'");self.service.db.commit()
        self.transport.s['messages'].append({'id':'m2','direction':'inbound','text':'1888072行程','kind':'text'})
        self.service.ingest('a',self.transport.s)
        self.service.policy('a','auto')
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM tasks WHERE status='pending'").fetchone()[0],0)

    def test_approval_expired_or_source_changed_is_invalid(self):
        from tongcheng_document_review import approved_documents
        self.approve();reviews={'doc':self.service.approved_documents()[0]['approval']}
        future=datetime.now(timezone.utc)+timedelta(days=2)
        self.assertEqual(approved_documents([self.doc],reviews,future),[])
        self.doc['source_sha256']='new-import';self.assertEqual(self.service.approved_documents(),[])

    async def test_live_authority_is_not_fabricated_customer_confirmation(self):
        self.approve();self.service.production_authorized=lambda:True
        self.service.policy('a','auto')
        self.assertFalse(self.service.overview()['real_send_verified'])
        self.transport.s.update(identity_source='platform_websocket',conversation_id='ws:new')
        self.transport.s['messages']=[{'id':'new','kind':'text','direction':'inbound','text':'1888072行程','newly_received':True}]
        self.assertEqual(len(self.service.ingest('a',self.transport.s)),1)
        self.assertEqual(self.service.ingest('a',self.transport.s),[])

    async def test_only_missing_condition_can_send_without_factual_source(self):
        self.approve();self.service.production_authorized=lambda:True;self.service.production_revision=lambda:'launch1'
        text='大人多少钱？小孩多少钱？'
        self.transport.s['messages'][0]['text']=text
        self.service.db.execute("UPDATE messages SET text=? WHERE id='m1'",(text,));self.service.db.commit()
        result={'status':'shadow_clarification_ready','text':'请问计划哪天出游呀？','evidence':[]}
        with patch('workbench_core.shadow_documents.document_draft',return_value=result):
            self.assertEqual((await self.service.generate_task(self.task['id'],self.config))['status'],'draft_ready')
        self.assertTrue((await self.send()).accepted);self.assertEqual(self.transport.calls,1)

    async def test_revoked_launch_blocks_prepared_clarification(self):
        self.approve();self.service.production_authorized=lambda:True;self.service.production_revision=lambda:'launch1'
        text='多人出游有优惠吗？'
        self.transport.s['messages'][0]['text']=text
        self.service.db.execute("UPDATE messages SET text=? WHERE id='m1'",(text,));self.service.db.commit()
        result={'status':'shadow_clarification_ready','text':'一共几位出游呀？','evidence':[]}
        with patch('workbench_core.shadow_documents.document_draft',return_value=result):
            await self.service.generate_task(self.task['id'],self.config)
        self.service.production_authorized=lambda:False
        self.assertFalse((await self.send()).accepted);self.assertEqual(self.transport.calls,0)

    def test_clarification_cannot_repeat_known_or_already_asked_condition(self):
        from tongcheng_production import clarification_slot
        question='多少钱？'
        history=[{'direction':'inbound','text':'30号去，多少钱？'}]
        self.assertIsNone(clarification_slot(question,history,'请问计划哪天出游呀？'))
        history=[{'direction':'outbound','text':'请问计划哪天出游呀？'},{'direction':'inbound','text':question}]
        self.assertIsNone(clarification_slot(question,history,'请问计划哪天出游呀？'))
        self.assertIsNone(clarification_slot(question,[],'儿童免费，请问计划哪天出游呀？'))
        history=[{'direction':'inbound','text':'我一大一小，小朋友五岁，十一月八号出发，多少钱？'}]
        self.assertIsNone(clarification_slot(question,history,'请问计划哪天出游呀？'))
        self.assertIsNone(clarification_slot(question,history,'请问小朋友多大了？'))

    def test_required_condition_prompt_does_not_guess_business_rules_or_repeat(self):
        from tongcheng_production import required_clarification
        history=[{'direction':'inbound','text':'大人多少钱？小孩多少钱？'}]
        self.assertEqual(required_clarification(history[0]['text'],history),'请问计划哪天出游呀？')
        self.assertIsNone(required_clarification('优惠券为什么用不上',history))
        history=[{'direction':'inbound','text':'一大一小，孩子五岁，十一月八号出发，多少钱？'}]
        self.assertIsNone(required_clarification(history[0]['text'],history))

    def test_library_grant_is_version_bound_and_expiring(self):
        from tongcheng_production import library_documents
        grant={'enabled':True,'authority':'explicit_user_live_launch','revision':'launch','expires_at':self.expiry,'source_sha256':'version','rows':{'doc':'hash'}}
        self.assertEqual(len(library_documents([self.doc],grant)),1)
        self.doc['content_hash']='changed';self.assertEqual(library_documents([self.doc],grant),[])
        self.doc['content_hash']='hash';grant['enabled']=False;self.assertEqual(library_documents([self.doc],grant),[])
        grant['enabled']=True;grant['expires_at']='2020-01-01T00:00:00+00:00';self.assertEqual(library_documents([self.doc],grant),[])


class ModelHealthTests(unittest.TestCase):
    def test_quota_circuit_and_model_switch(self):
        import tongcheng_model
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
            factory=httpx.Client;calls=[]
            def respond(request):
                calls.append(request)
                return httpx.Response(429,json={'error':{'code':'SetLimitExceeded','message':'synthetic-key must not persist'}})
            with patch.object(tongcheng_model.httpx,'Client',side_effect=lambda **kw:factory(transport=httpx.MockTransport(respond),**kw)):
                invoke=provider(root,config)
                with self.assertRaises(ValueError):invoke({})
                with self.assertRaisesRegex(ValueError,'model_circuit_open'):invoke({})
            self.assertEqual(len(calls),1);self.assertEqual(status(root,config)['state'],'unavailable')
            self.assertNotIn('synthetic-key',(root/'model-live-health.json').read_text())
            from dataclasses import replace
            self.assertEqual(status(root,replace(config,model='replacement'))['state'],'not_checked')
