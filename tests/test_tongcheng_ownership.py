import json
import tempfile
import unittest
from pathlib import Path
from tests.test_tongcheng import Transport
from tests.test_shadow_documents import document
from tongcheng import TongchengService
from tongcheng_direct import DirectTransport
from tongcheng_ownership import classify_outbound,passive,repair_roles,WELCOME_TEXTS,AWAY_TEXT,HANDOFF_NOTICE,customer_question
from workbench_core.knowledge import ModelConfig
from contracts import OutboundMessageCommand
from datetime import datetime,timezone,timedelta
from unittest.mock import patch


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.transport=Transport()
        self.service=TongchengService(self.root/'state.sqlite',self.transport);self.service.active=True
        self.service.policy('local','draft');self.service.ingest('local',self.transport.s)
        self.doc=document();self.service.shadow_documents=lambda:[self.doc];self.service.model_available=lambda:True
        self.service.review_document('doc',True,(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())

    def tearDown(self):self.service.db.close()

    def ingest(self,*messages):
        self.transport.s['messages'].extend(messages)
        return self.service.ingest('local',self.transport.s)

    def message(self,identifier,direction='inbound',text='1888072行程',**extra):
        return dict(id=identifier,direction=direction,text=text,kind='text',**extra)

    async def test_welcome_after_question_keeps_task_and_allows_checked_send(self):
        self.ingest(self.message('q'));task=self.service.db.execute('SELECT * FROM tasks').fetchone()
        self.ingest(self.message('welcome','outbound',next(iter(WELCOME_TEXTS))))
        self.assertFalse(self.service.is_human('local','c1'))
        self.assertEqual(self.service.db.execute('SELECT status FROM tasks').fetchone()[0],'human_review')
        self.assertEqual([m['id'] for m in self.service.dialogue_history('local','c1')],['q'])
        answer={'status':'shadow_draft_ready','text':'上海迪士尼三天行程。','evidence':[{'document_id':'doc','source_sha256':'version','quote':'上海迪士尼三天行程'}]}
        config=ModelConfig(enabled=True,endpoint='https://example.test/chat/completions',model='test',api_key='synthetic-key')
        with patch('workbench_core.shadow_documents.document_draft',return_value=answer):await self.service.generate_task(task['id'],config)
        result=await self.service.send_message(OutboundMessageCommand('local','c1',{'type':'text','text':answer['text']},task['id']))
        self.assertTrue(result.accepted);self.assertEqual(self.transport.calls,1)

    async def test_unknown_business_reply_still_takes_over_and_cancels(self):
        self.ingest(self.message('q'));self.ingest(self.message('human','outbound','今天下午我给您核对报价'))
        self.assertTrue(self.service.is_human('local','c1'))
        self.assertEqual(self.service.db.execute('SELECT status FROM tasks').fetchone()[0],'cancelled')

    async def test_explicit_handoff_survives_welcome_and_away_messages(self):
        self.service.handoff('local','c1',True)
        self.ingest(self.message('welcome','outbound',next(iter(WELCOME_TEXTS))),self.message('away','outbound',AWAY_TEXT),self.message('q'))
        self.assertTrue(self.service.is_human('local','c1'))

    def test_narrow_types_and_inbound_spoof_cannot_bypass_ownership(self):
        self.assertEqual(classify_outbound('text','您好'),'merchant_reply')
        self.assertEqual(classify_outbound('unsupported','',17,json.dumps({'bizType':'ai_agent_questions','data':[]})),'platform_question_card')
        coupon={'iframeType':2,'title':'30元旅游度假红包','usageRules':'可用于旅游度假产品'}
        self.assertEqual(classify_outbound('unsupported','',12,json.dumps(coupon)),'coupon_card')
        self.assertEqual(classify_outbound('unsupported','',1,'unknown-image'),'merchant_reply')
        self.assertFalse(passive({'direction':'inbound','kind':'text','text':next(iter(WELCOME_TEXTS))}))
        self.assertEqual(classify_outbound('text',next(iter(WELCOME_TEXTS))+'成人只要100元'),'merchant_reply')

    async def test_origin_repair_never_reopens_tasks_or_explicit_handoffs(self):
        wire=DirectTransport(self.root);self.addAsyncCleanup(wire.close)
        self.service.audit('production_user_authorized_launch','launch')
        self.service.handoff('local','ws:one',True)
        text=next(iter(WELCOME_TEXTS));now=datetime.now(timezone.utc).isoformat()
        wire.db.execute('INSERT INTO inbox VALUES(?,?,?,?,?,?)',('one','welcome','outbound','text',text,now));wire.db.commit()
        self.assertEqual(repair_roles(self.service,wire)['released'],0)
        self.assertTrue(self.service.is_human('local','ws:one'))
        self.service.db.execute("INSERT INTO sessions VALUES('local','ws:two',1)");self.service.db.commit()
        wire.db.execute('INSERT INTO inbox VALUES(?,?,?,?,?,?)',('two','welcome','outbound','text',text,now));wire.db.commit()
        self.assertEqual(repair_roles(self.service,wire)['released'],1)
        self.assertFalse(self.service.is_human('local','ws:two'))
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

    async def test_handoff_notice_is_real_queue_not_business_answer_and_never_retried(self):
        self.service.production_authorized=lambda:True
        self.service.policy('local','auto')
        self.ingest(self.message('q',text='这个问题没有对应资料'))
        task=self.service.db.execute('SELECT * FROM tasks').fetchone()
        self.assertFalse(self.service.prepare_handoff_notice(task['id'],'source_insufficient'))
        case=self.service.db.execute('SELECT * FROM handoff_cases').fetchone()
        self.assertEqual(case['status'],'open');self.assertIsNone(case['notified_at'])
        self.assertEqual(len(self.service.overview()['handoffs']),1)
        self.assertEqual(self.transport.calls,0)
        # A new question updates the human queue but does not spam the same notice.
        self.ingest(self.message('next',text='我补充一个条件'))
        next_task=self.service.db.execute("SELECT id FROM tasks WHERE message='next'").fetchone()[0]
        self.assertFalse(self.service.prepare_handoff_notice(next_task,'source_insufficient'))
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM handoff_cases WHERE status='open'").fetchone()[0],1)
        self.service.handoff('local','c1',False)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM handoff_cases WHERE status='open'").fetchone()[0],0)

    async def test_unavailable_model_does_not_send_notice_but_retains_human_queue(self):
        self.service.production_authorized=lambda:True;self.service.policy('local','auto')
        self.ingest(self.message('q'));task=self.service.db.execute('SELECT id FROM tasks').fetchone()[0]
        self.service.model_available=lambda:False
        self.assertFalse(self.service.prepare_handoff_notice(task,'model_error'))
        self.assertEqual(self.transport.calls,0)
        self.assertEqual(self.service.db.execute('SELECT status FROM handoff_cases').fetchone()[0],'open')

    def test_previous_attachment_does_not_discard_new_text(self):
        self.assertEqual(customer_question([{'direction':'inbound','kind':'unsupported','text':''},{'direction':'inbound','kind':'text','text':'我一个人'}]),'我一个人')
        self.assertIsNone(customer_question([{'direction':'inbound','kind':'unsupported','text':''}]))

    async def test_welcome_between_question_and_product_card_does_not_break_enrichment(self):
        self.ingest(self.message('q',received_at='2026-10-29T05:00:00+00:00'))
        self.ingest(self.message('welcome','outbound',next(iter(WELCOME_TEXTS)),received_at='2026-10-29T05:00:01+00:00'))
        card={'id':'card','direction':'inbound','kind':'card','text':'商品卡','received_at':'2026-10-29T05:00:02+00:00','products':[{'external_product_id':'1888072','title':'上海迪士尼三天行程'}]}
        events=self.ingest(card)
        self.assertEqual(len(events),1);self.assertEqual(events[0].external_message_id,'q')
        self.assertNotEqual(self.service.db.execute("SELECT status FROM tasks WHERE message='q'").fetchone()[0],'cancelled')
