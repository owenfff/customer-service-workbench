import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from tests.test_tongcheng import Transport
from tongcheng import TongchengService
import tongcheng_app


class ReplyLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.transport=Transport();self.service=TongchengService(self.root/'state.sqlite',self.transport)
        self.service.active=True;self.service.ingest('local',self.transport.s)
        self.original_service=getattr(tongcheng_app.app.state,'service',None)
        tongcheng_app.app.state.service=self.service
        self.event=SimpleNamespace(external_conversation_id='c1',external_message_id='new',content={'type':'text'})

    def tearDown(self):
        tongcheng_app.app.state.service=self.original_service
        self.service.db.close();self.temp.cleanup()

    def ingest(self):
        self.transport.s['messages']=[{'id':'new','direction':'inbound','kind':'text','text':'咨询行程'}]
        self.service.ingest('local',self.transport.s)

    async def test_human_owned_message_never_calls_model_or_reports_model_error(self):
        self.service.handoff('local','c1',True);self.ingest()
        with patch.object(self.service,'generate_task',new=AsyncMock()) as generate:
            await tongcheng_app.process_reply(None,self.event)
        generate.assert_not_awaited()
        row=self.service.db.execute("SELECT action,details FROM audit WHERE action='reply_generation_skipped'").fetchone()
        self.assertEqual(json.loads(row['details'])['reason'],'human_takeover')
        self.assertIsNone(self.service.db.execute("SELECT 1 FROM audit WHERE action='reply_generation_failed'").fetchone())

    async def test_controlled_race_and_real_error_have_distinct_safe_log_reasons(self):
        self.ingest();(self.root/'model.json').write_text('{}','utf-8')
        for error,action,reason in [(ValueError('draft_changed'),'reply_generation_skipped','draft_changed'),(ValueError('private-secret-error'),'reply_generation_failed','model_error')]:
            with patch.object(tongcheng_app,'PRIVATE',self.root),patch.object(tongcheng_app,'model_is_available',return_value=True),patch.object(self.service,'generate_task',new=AsyncMock(side_effect=error)):
                await tongcheng_app.process_reply(None,self.event)
            row=self.service.db.execute('SELECT details FROM audit WHERE action=? ORDER BY id DESC LIMIT 1',(action,)).fetchone()
            self.assertEqual(json.loads(row[0])['reason'],reason);self.assertNotIn('private-secret',row[0])
