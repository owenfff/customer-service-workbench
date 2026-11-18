import tempfile
import unittest
from pathlib import Path
from tests.test_tongcheng import Transport
from tongcheng import TongchengService
from tongcheng_feishu import FeishuNotifications


class FeishuHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.service=TongchengService(self.root/'state.sqlite',Transport());self.service.active=True
        self.service.db.execute("INSERT INTO policies VALUES('local','auto','[]')")
        self.calls=[];self.result={'ok':True,'reason':'accepted'}
        def sender(url,text,**kw):self.calls.append(text);return self.result
        self.sender=sender
        self.notifications=FeishuNotifications(self.root,self.service,Transport(),sender)

    def tearDown(self):
        self.notifications.close();self.service.db.close();self.temp.cleanup()

    def task(self,identifier,reason='requires_human',status='human_review'):
        self.service.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',('local','ws:customer',identifier,'inbound','是否含早餐？','text'))
        self.service.db.execute('INSERT INTO tasks(id,account,conversation,message,text,status,reason) VALUES(?,?,?,?,?,?,?)',(identifier,'local','ws:customer',identifier,'',status,reason));self.service.db.commit()

    def enable(self):
        return self.notifications.save(True,'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-test-hook','synthetic-secret')

    async def test_no_backlog_no_premature_notification_and_dedup_survives_restart(self):
        self.task('old');status=self.enable()
        self.assertNotIn('synthetic',str(status))
        self.task('new','human_or_knowledge_required')
        await self.notifications.tick();self.assertEqual(self.calls,[])
        self.service.db.execute("UPDATE tasks SET reason='requires_human' WHERE id='new'");self.service.db.commit()
        await self.notifications.tick();await self.notifications.tick();self.assertEqual(len(self.calls),1)
        self.assertIn('是否含早餐？',self.calls[0]);self.assertIn('原因：',self.calls[0])
        self.notifications.close();self.notifications=FeishuNotifications(self.root,self.service,Transport(),self.sender)
        await self.notifications.tick();self.assertEqual(len(self.calls),1)

    async def test_ambiguous_network_result_is_not_retried(self):
        self.enable();self.task('new');self.result={'ok':False,'reason':'transport_failed'}
        await self.notifications.tick();await self.notifications.tick()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.notifications.status()['last_delivery']['status'],'uncertain')

    async def test_shadow_and_paused_and_human_owned_do_not_alert(self):
        self.enable();self.task('new')
        self.service.db.execute("UPDATE policies SET mode='shadow'");self.service.db.commit()
        await self.notifications.tick();self.assertEqual(self.calls,[])
        self.service.db.execute("UPDATE policies SET mode='auto'");self.service.db.commit();self.service.active=False
        await self.notifications.tick();self.assertEqual(self.calls,[])
        self.service.active=True;self.service.handoff('local','ws:customer',True)
        await self.notifications.tick();self.assertEqual(self.calls,[])

    async def test_configured_test_contains_no_customer_data(self):
        self.enable();result=await self.notifications.test()
        self.assertTrue(result['ok']);self.assertNotIn('早餐',self.calls[0])
        self.assertIn('人工提醒测试',self.calls[0]);self.assertIn('北京时间',self.calls[0])

    async def test_accepted_handoff_notice_does_not_hide_open_human_queue(self):
        self.enable();self.task('new')
        self.service.production_authorized=lambda:True;self.service.model_available=lambda:True
        self.assertFalse(self.service.prepare_handoff_notice('new','source_insufficient'))
        self.service.db.execute("UPDATE tasks SET status='server_accepted' WHERE id='new'");self.service.db.commit()
        await self.notifications.tick()
        self.assertEqual(len(self.calls),1);self.assertIn('产品资料缺少对应规则',self.calls[0])
