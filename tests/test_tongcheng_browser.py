"""Real browser against a routed synthetic page. Never accesses Tongcheng servers."""
import sys
import tempfile
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from tongcheng_browser import BrowserTransport

HTML='''<meta charset="utf-8"><div id="imDialog"><div id="im-dialog-header"><span class="nickname">同名客户</span></div>
<ul class="user-list"><li class="user-item active"><h3>同名客户</h3></li></ul>
<div class="message"><div class="msgBox" data-index="inbound-1"><div class="session-item flex-left"><div class="info">同名客户 今天 12:00:00</div><div class="text-msg">你好</div></div></div></div>
<textarea></textarea><button id="send">发 送</button></div>
<script>document.querySelector('#send').onclick=()=>{const text=document.querySelector('textarea').value;const row=document.createElement('div');row.className='msgBox';row.dataset.index='echo-1';row.innerHTML='<div class="session-item flex-right"><div class="info">今天 12:00:01</div><div class="text-msg"></div></div>';row.querySelector('.text-msg').textContent=text;document.querySelector('.message').append(row);document.querySelector('textarea').value='';};</script>'''

class BrowserProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.runtime=await async_playwright().start()
        self.browser=await self.runtime.chromium.launch(channel='msedge',headless=True)
        self.context=await self.browser.new_context()
        await self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=HTML))
        self.page=await self.context.new_page()
        await self.page.goto('https://kefu.17u.cn/synthetic-test')
        self.t=BrowserTransport({'calibrated':True,'layout':'fenghuo_observed_20261023','editor':'#imDialog textarea','send':'#send'},Path(self.temp.name)/'profile')
        self.t.context=self.context;self.t.page=self.page

    async def asyncTearDown(self):
        if self.t.references:self.t.references.close()
        await self.browser.close();await self.runtime.stop();self.temp.cleanup()

    async def test_system_warning_is_not_customer_message(self):
        await self.page.locator('.message').evaluate("e=>{const r=document.createElement('div');r.className='msgBox';r.dataset.index='system-warning';r.innerHTML='<div class=\"session-item flex-left\"><div class=\"info\">警告：为了确保交易的安全性和合法性，请勿引导用户线下交易。</div></div>';e.append(r)}")
        snap=await self.t.snapshot()
        self.assertTrue(snap['send_identity_verified'])
        self.assertEqual(snap['messages'][-1]['kind'],'system')
        self.assertEqual(snap['messages'][-1]['direction'],'unknown')
        await self.page.locator('.message .msgBox').last.locator('.info').evaluate("e=>e.textContent='同名客户领取了你的红包'")
        snap=await self.t.snapshot()
        self.assertTrue(snap['send_identity_verified'])
        self.assertEqual(snap['messages'][-1]['kind'],'red_packet')

    async def test_sdk_websocket_submission_keeps_echo_verification(self):
        url='https://file.40017.cn/vacation_feresource/im/pc/assets/im-test.js'
        script="""export const t=()=>({currentChatUserId:'raw-customer-1',currentMessages:[{fingerPrint:'inbound-1'}]});
        export const n={isOnline:()=>true,on:(name,fn)=>{if(name==='received-message')window.injectWire=fn},sendMsg:args=>{window.sdkSends=(window.sdkSends||0)+1;
        const row=document.createElement('div');row.className='msgBox';row.dataset.index=args.fingerPrint;
        row.innerHTML='<div class="session-item flex-right"><div class="info">今天 12:00:01</div><div class="text-msg"></div></div>';
        row.querySelector('.text-msg').textContent=args.msgContent;document.querySelector('.message').append(row);return Promise.resolve({});}};"""
        await self.context.route(url,lambda route:route.fulfill(status=200,content_type='application/javascript',headers={'Access-Control-Allow-Origin':'*'},body=script))
        await self.page.evaluate('(url)=>import(url)',url)
        self.t.config['websocket_bridge']=True
        snap=await self.t.snapshot()
        self.assertEqual(snap['sdk_customer_id'],'raw-customer-1')
        echo=await self.t.send_checked(snap['conversation_id'],'inbound-1','已审核回复')
        self.assertTrue(echo)
        self.assertEqual(await self.page.evaluate('window.sdkSends'),1)
        self.assertEqual(await self.page.locator('textarea').input_value(),'')
        await self.page.evaluate("()=>window.injectWire({typeu:3,fp:'wire-new',from:'raw-customer-1',to:'merchant',dataContent:JSON.stringify({ty:0,m:'新咨询'})})")
        await self.t.snapshot()
        self.assertEqual(self.t.wire_pending[0]['message_id'],'wire-new')
        await self.page.evaluate("async url=>{const m=await import(url);m.n.sendMsg=()=>{window.sdkSends++;return Promise.resolve({})}}",url)
        self.assertIsNone(await self.t.send_checked(snap['conversation_id'],echo,'没有回显的测试'))
        self.assertEqual(await self.page.evaluate('window.sdkSends'),2)
        self.assertEqual(await self.page.locator('.msgBox').count(),2)

    async def test_text_direction_and_outbound_echo(self):
        snap=await self.t.snapshot()
        self.assertEqual(snap['messages'][0]['direction'],'inbound')
        echo=await self.t.send_checked(snap['conversation_id'],'inbound-1','你好，在的哦')
        self.assertEqual(echo,'echo-1')
        self.assertEqual((await self.t.snapshot())['messages'][-1]['direction'],'outbound')

    async def test_human_input_is_preserved(self):
        snap=await self.t.snapshot()
        await self.page.locator('textarea').fill('客服正在编辑')
        with self.assertRaises(ValueError):await self.t.send_checked(snap['conversation_id'],'inbound-1','自动回复')
        self.assertEqual(await self.page.locator('textarea').input_value(),'客服正在编辑')
        self.assertEqual(await self.page.locator('.msgBox').count(),1)

    async def test_same_name_different_customer_cannot_receive_old_reply(self):
        first=await self.t.snapshot()
        await self.page.set_content(HTML.replace('inbound-1','another-customer-message'))
        second=await self.t.snapshot()
        self.assertNotEqual(first['conversation_id'],second['conversation_id'])
        with self.assertRaises(ValueError):await self.t.send_checked(first['conversation_id'],'inbound-1','旧客户回复')
        self.assertEqual(await self.page.locator('.msgBox').count(),1)

    async def test_new_tail_blocks_old_reply(self):
        first=await self.t.snapshot()
        await self.page.set_content(HTML.replace('inbound-1','new-inbound'))
        with self.assertRaises(ValueError):await self.t.send_checked(first['conversation_id'],'inbound-1','旧草稿')

    async def test_header_updated_but_previous_customer_messages_still_rendered(self):
        await self.page.set_content(HTML.replace('class="info">同名客户','class="info">另一个客户'))
        with self.assertRaisesRegex(ValueError,'message_sender'):
            await self.t.snapshot()
