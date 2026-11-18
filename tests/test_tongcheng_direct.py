import asyncio
import json
import sys
import tempfile
import time
import unittest
from unittest.mock import patch,AsyncMock,MagicMock
import websockets
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from tongcheng_wire import fields,packet,blob,integer,login_body,text_packet
from tongcheng_direct import DirectTransport

class WireTests(unittest.TestCase):
    def test_product_card_requires_matching_platform_id_and_excludes_price(self):
        from tongcheng_products import product_card
        row={'serialId':'1282759','title':'南京深度游','detailLink':'https://wx.17u.cn/journey/tours/1282759?version=A','setMeal':'A套餐','price':'958','titleTagHTML':'<b>营销</b>'}
        content={'bizType':'product_simple_card','data':[row]}
        product=product_card(json.dumps(content))[0]
        self.assertEqual(product['external_product_id'],'1282759');self.assertNotIn('958',json.dumps(product))
        self.assertNotIn('titleTagHTML',product)
        row['serialId']='9999999';self.assertEqual(product_card(content),[])
        row['serialId']='1282759';row['detailLink']='https://evil.test/journey/tours/1282759'
        self.assertEqual(product_card(content),[])
        row['detailLink']='https://m.ly.com/journey/tours/1282759'
        self.assertEqual(product_card(content),[])
        row['wechatProgramUrl']='https://wx.17u.cn/journey/tours/1282759?version=D'
        self.assertEqual(product_card(content)[0]['external_product_id'],'1282759')
        row['wechatProgramUrl']='https://wx.17u.cn/journey/tours/9999999'
        self.assertEqual(product_card(content),[])
        self.assertIsNone(product_card({'bizType':'unknown','data':[]}))
    def test_schema_roundtrip_and_truncation(self):
        self.assertEqual(fields(packet(123,46,blob(1,'中文'))),{1:123,2:46,3:blob(1,'中文')})
        for invalid in (b'\x08\x80',b'\x1a\x04a',b'\x00'):
            with self.assertRaises(ValueError):fields(invalid)
    def test_text_uses_exact_customer_and_fingerprint(self):
        data=json.loads(fields(text_packet({'source_uid':'merchant','tenant_code':'tenant'},'customer','回复','unique'))[1])
        self.assertEqual(data['to'],'customer');self.assertEqual(data['fp'],'unique')
        self.assertEqual(json.loads(data['dataContent'])['ty'],0)

class DirectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.t=DirectTransport(self.tmp.name)
        self.t.session={'source_uid':'merchant','tenant_code':'tenant'}
        self.t.account_verified=True;self.t.wire_status['online']=True
    async def asyncTearDown(self):await self.t.close();self.tmp.cleanup()
    def inbound(self,customer='customer',mid='msg',target='merchant'):
        msg={'typeu':3,'from':customer,'to':target,'fp':mid,'dataContent':json.dumps({'ty':0,'m':'你好','nickname':'同名客户'})}
        return fields(packet(1,46,blob(1,json.dumps(msg)))+integer(4,int(time.time()*1000)))
    async def test_durable_dedup_scoped_customers_and_wrong_recipient(self):
        self.t.receive(self.inbound());self.t.receive(self.inbound())
        self.t.receive(self.inbound('other','msg'));self.t.receive(self.inbound('stranger','bad','someone-else'))
        self.assertEqual(self.t.db.execute('SELECT count(*) FROM inbox').fetchone()[0],2)
        snap=await self.t.observe_next();self.assertEqual(snap['conversation_id'],'ws:customer')
        self.assertEqual(snap['message_id_source'],'platform_websocket_fingerprint')
        # Delivery is acknowledged to the transport only on the next poll.
        self.assertEqual(self.t.db.execute('SELECT dirty FROM customers WHERE id="customer"').fetchone()[0],1)
        await self.t.observe_next()
        self.assertEqual(self.t.db.execute('SELECT dirty FROM customers WHERE id="customer"').fetchone()[0],0)
    async def test_server_acceptance_is_not_echo_and_no_send_replay(self):
        self.t.receive(self.inbound());await self.t.locate('ws:customer')
        sent=[]
        class Socket:
            async def send(_,data):
                msg=fields(data);sent.append(msg)
                self.t.pending[msg[1]].set_result({1:msg[1],5:20001})
        self.t.socket=Socket()
        result=await self.t.send_checked('ws:customer','msg','回复')
        self.assertEqual(result['kind'],'server_accepted');self.assertEqual(len(sent),1)
        with self.assertRaises(ValueError):await self.t.send_checked('ws:customer','msg','旧回复')
        self.assertEqual(len(sent),1)

    async def test_passive_welcome_does_not_change_question_send_target(self):
        from tongcheng_ownership import WELCOME_TEXTS
        self.t.receive(self.inbound())
        message={'typeu':3,'from':'merchant','to':'customer','fp':'welcome','dataContent':json.dumps({'ty':0,'m':next(iter(WELCOME_TEXTS))})}
        self.t.receive({3:blob(1,json.dumps(message))})
        class Socket:
            async def send(_,data):
                msg=fields(data);self.t.pending[msg[1]].set_result({1:msg[1],5:20001})
        self.t.socket=Socket()
        self.assertEqual((await self.t.send_checked('ws:customer','msg','回复'))['kind'],'server_accepted')
        with self.assertRaises(ValueError):await self.t.send_checked('ws:customer','msg','不能重复发送')

    async def test_product_card_restores_context_without_new_message_or_cross_customer_leak(self):
        card={'bizType':'product_simple_card','data':[{'serialId':'1282759','title':'南京深度游','detailLink':'https://wx.17u.cn/journey/tours/1282759'}]}
        message={'typeu':3,'from':'customer','to':'merchant','fp':'card','dataContent':json.dumps({'ty':17,'m':json.dumps(card)})}
        self.t.receive({3:blob(1,json.dumps(message))});self.t.receive(self.inbound(mid='question'))
        snap=await self.t.locate('ws:customer')
        self.assertEqual(snap['products'][0]['external_product_id'],'1282759')
        self.assertEqual(snap['messages'][0]['kind'],'card')
        self.t.receive(self.inbound(customer='other'));self.assertEqual((await self.t.locate('ws:other'))['products'],[])
        with self.t.db:
            self.t.db.execute('DELETE FROM product_cards')
            self.t.db.execute("UPDATE inbox SET kind='unsupported',text='' WHERE id='card'")
        await self.t.close();self.t=DirectTransport(self.tmp.name)
        restored=self.t.customer_snapshot('customer')
        self.assertEqual(restored['products'][0]['external_product_id'],'1282759')
        self.assertEqual(restored['messages'][0]['kind'],'card')
        self.assertFalse(any(m['newly_received'] for m in restored['messages']))

    async def test_string_text_type_and_control_events_do_not_create_consultations(self):
        def event(ty,mid,text):
            body={'typeu':'3','from':'customer','to':'merchant','fp':mid,'dataContent':json.dumps({'ty':ty,'m':text})}
            return {3:blob(1,json.dumps(body))}
        self.t.receive(event('0','text','有游船吗'))
        for ty in (11,90,91,92,93,990):self.t.receive(event(ty,str(ty),'状态'))
        self.assertEqual(self.t.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
        self.assertEqual(self.t.db.execute('SELECT text FROM inbox').fetchone()[0],'有游船吗')
        self.assertEqual(self.t.wire_status['control_events'],6)
        self.assertEqual(self.t.db.execute('SELECT count(*) FROM observations').fetchone()[0],7)

    async def test_independent_login_receive_send_and_restart_without_browser(self):
        self.t.session.update({'userId':123,'spaceId':'space','appId':'app','deviceId':'test-device','accessToken':'synthetic-only','accessTokenExpireInTimestamp':time.time()+7200})
        self.t.save_session()
        async def server(socket):
            login=fields(await socket.recv())
            self.assertEqual(login[2],50)
            await socket.send(packet(login[1],50)+integer(5,20001))
            await socket.send(packet(999,46,self.inbound()[3])+integer(4,int(time.time()*1000)))
            async for raw in socket:
                msg=fields(raw)
                if msg[2]==46:await socket.send(packet(msg[1],46)+integer(5,20001))
        original=websockets.connect
        async with websockets.serve(server,'127.0.0.1',0) as host:
            port=host.sockets[0].getsockname()[1]
            def connect(*args,**kwargs):return original('ws://127.0.0.1:'+str(port),**kwargs)
            with patch('tongcheng_direct.websockets.connect',connect):
                await self.t.open()
                for _ in range(100):
                    if self.t.db.execute('SELECT count(*) FROM inbox').fetchone()[0]:break
                    await asyncio.sleep(.01)
                self.assertIsNone(self.t.context)
                snap=await self.t.observe_next();self.assertEqual(snap['messages'][0]['text'],'你好')
                receipt=await self.t.send_checked('ws:customer','msg','回复')
                self.assertEqual(receipt['kind'],'server_accepted')
                await self.t.close()
                self.t=DirectTransport(self.tmp.name)
                await self.t.open()
                self.assertTrue((await self.t.snapshot())['logged_in'])
                self.assertEqual(self.t.db.execute('SELECT count(*) FROM inbox').fetchone()[0],2)

    async def test_refresh_persists_only_in_private_session_and_rejection_requires_login(self):
        self.t.session.update({'refreshToken':'synthetic-refresh','accessTokenExpireInTimestamp':0})
        response=MagicMock(status_code=200)
        response.json.return_value={'code':200,'data':{'accessToken':'renewed','accessTokenExpireInTimestamp':time.time()+7200}}
        client=AsyncMock();client.post.return_value=response
        with patch('tongcheng_direct.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            await self.t.refresh()
        self.assertEqual(json.loads((Path(self.tmp.name)/'wire-session.json').read_text())['accessToken'],'renewed')
        self.t.session['accessTokenExpireInTimestamp']=0;response.json.return_value={'code':401}
        with patch('tongcheng_direct.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            with self.assertRaisesRegex(ValueError,'reauthentication_required'):await self.t.refresh()

    async def test_business_comparison_reports_socket_gap_without_ingesting_or_sending(self):
        (Path(self.tmp.name)/'business-session.json').write_text(json.dumps({'token':'synthetic-only','account':'merchant'}))
        response=MagicMock(status_code=200)
        response.json.return_value={'code':10000,'data':[{'srcUserUid':'customer','lastMsgSrcUserUid':'customer','msgType':0,'msgTime2':int((time.time()+1)*1000),'lastMsgFingerprint':'missing'}]}
        client=AsyncMock();client.post.return_value=response
        with patch('tongcheng_direct.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            await self.t.probe_business()
        self.assertEqual(self.t.wire_status['business_probe']['state'],'socket_gap_detected')
        self.assertEqual(self.t.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertTrue(client.post.call_args.args[0].endswith('/core/session/list'))
        self.assertNotIn('synthetic-only',json.dumps(self.t.wire_status))

    async def test_legacy_red_packet_notice_does_not_block_other_customers(self):
        with self.t.db:
            self.t.db.execute("INSERT INTO inbox VALUES('notice-only','notice','inbound','text','领取了红包',NULL)")
            self.t.db.execute("INSERT INTO observations VALUES('notice-only','notice','2026-10-27','11','str','\"领取了红包\"')")
            self.t.db.execute("INSERT INTO customers VALUES('notice-only','notice',1)")
        await self.t.close();self.t=DirectTransport(self.tmp.name)
        self.t.session={'source_uid':'merchant','tenant_code':'tenant'}
        self.t.receive(self.inbound('real-customer'))
        self.assertIsNone(await self.t.observe_next())
        self.assertEqual((await self.t.observe_next())['conversation_id'],'ws:real-customer')
        self.assertEqual(self.t.db.execute("SELECT kind FROM inbox WHERE id='notice'").fetchone()[0],'system')
