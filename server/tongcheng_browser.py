"""Dedicated persistent Chromium profile; selectors require local DOM calibration."""
import asyncio
from urllib.parse import urlsplit
from urllib.parse import parse_qs
import hashlib
from pathlib import Path
import json
import sqlite3
import re
from uuid import uuid4
from datetime import datetime,timedelta,timezone

def rendered_time(text):
    zone=timezone(timedelta(hours=8));current=datetime.now(zone)
    found=re.search(r'(今天|昨天|\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)',text)
    if not found:return None
    day,clock=found.groups()
    try:
        date=(current-timedelta(days=1 if day=='昨天' else 0)).date() if day in ('今天','昨天') else datetime.strptime(day,'%Y/%m/%d').date()
        time=datetime.strptime(clock,'%H:%M:%S' if clock.count(':')==2 else '%H:%M').time()
        return datetime.combine(date,time,tzinfo=zone).isoformat()
    except ValueError:return None


def resolve_anchor_reference(db,message_ids):
    ids=list(message_ids)
    if not ids or len(set(ids))!=len(ids):raise ValueError('invalid_message_anchors')
    known=set()
    for mid in ids:
        known.update(r[0] for r in db.execute('SELECT conversation FROM anchors WHERE message=?',(mid,)))
    if len(known)>1:raise ValueError('conflicting_message_anchors')
    ref=next(iter(known)) if known else 'local-anchors:'+uuid4().hex
    with db:
        for mid in ids:db.execute('INSERT OR IGNORE INTO anchors VALUES(?,?)',(mid,ref))
    return ref


class BrowserTransport:
    def __init__(self, config, profile):
        self.config, self.profile = config, profile
        self.context = self.page = None
        self.references=None
        self.account_verified=False
        self.scan_index=0
        self.wire_status={}
        self.wire_customers={}
        self.wire_pending=[]

    async def open(self):
        from playwright.async_api import async_playwright
        self.runtime = await async_playwright().start()
        channel=self.config.get('browser_channel')
        if not channel and Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').exists():channel='msedge'
        try:
            self.context = await self.runtime.chromium.launch_persistent_context(str(self.profile), headless=False,channel=channel)
        except Exception:
            await self.runtime.stop()
            raise
        homes=[p for p in self.context.pages if urlsplit(p.url).hostname=='ebk.17u.cn' and urlsplit(p.url).path=='/vacation/']
        self.page = homes[0] if homes else await self.context.new_page()
        await self.page.goto('https://ebk.17u.cn/vacation/')
        try:
            account=self.page.get_by_text(re.compile(r'主账号[：:]'))
            await account.wait_for(state='visible',timeout=8000)
            expected=self.config.get('expected_account')
            account_text=await account.locator('..').inner_text() if await account.count()==1 else ''
            actual=account_text.replace('：',':').partition(':')[2].strip()
            self.account_verified=bool(expected and actual==expected)
            link=self.page.locator('a[href*="kefu.17u.cn/fenghuoweb/pc/"]')
            await link.wait_for(state='visible',timeout=8000)
            chats=[p for p in self.context.pages if urlsplit(p.url).hostname=='kefu.17u.cn']
            if not chats and await link.count()==1:await link.click()
        except Exception:
            pass  # Login/captcha remains an explicit user action.

    async def snapshot(self):
        if not self.context:
            raise ValueError('browser_not_started')
        pages = [p for p in self.context.pages if urlsplit(p.url).hostname == 'kefu.17u.cn']
        if not pages:
            return {'logged_in':False}
        if len(pages)>1:
            # Never silently discard a draft in a restored browser tab.
            for p in pages:
                editor=p.locator('#imDialog textarea')
                if await editor.count()==1 and await editor.input_value():raise ValueError('multiple_chat_tabs_with_human_input')
        self.page = pages[-1]
        c = self.config
        if c.get('expected_account') and not self.account_verified:
            return {'logged_in':False,'reason':'account_identity_unverified'}
        if not c.get('calibrated'):
            raise ValueError('browser_not_calibrated')
        if c.get('layout') == 'fenghuo_observed_20261023':
            return await self.observed_snapshot()
        # Only rendered DOM attributes; never reads tokens, storage, or app internals.
        root = self.page.locator(c['conversation_root'])
        if await root.count() != 1:
            raise ValueError('ambiguous_conversation')
        identity = await root.get_attribute(c['conversation_id_attribute'])
        rows = root.locator(c['message'])
        messages = []
        for row in await rows.all():
            mid = await row.get_attribute(c['message_id_attribute'])
            direction = await row.get_attribute(c['direction_attribute'])
            if not mid or direction not in c['directions']:
                raise ValueError('ambiguous_message')
            text = row.locator(c['text'])
            messages.append({'id':mid,'direction':c['directions'][direction],
                'kind':'text' if await text.count() == 1 else 'unsupported',
                'text':await text.inner_text() if await text.count() == 1 else ''})
        editor = self.page.locator(c['editor'])
        if await editor.count() != 1:
            raise ValueError('ambiguous_editor')
        value = await editor.input_value() if c.get('editor_is_input',True) else await editor.inner_text()
        return {'logged_in':True,'identity_verified':bool(identity),'identity_source':'platform_dom',
            'conversation_id':identity,'messages':messages,'input_text':value}

    async def observed_snapshot(self):
        """Rendered DOM only; encrypted order-page references are local identities."""
        title=self.page.locator('#im-dialog-header .nickname')
        active=self.page.locator('#imDialog .user-item.active h3')
        if await title.count()!=1 or await active.count()!=1:raise ValueError('ambiguous_selected_customer')
        label=await title.inner_text()
        if not label or label!=await active.inner_text():raise ValueError('customer_switch_in_progress')
        editor=self.page.locator('#imDialog textarea')
        if await editor.count()!=1:raise ValueError('ambiguous_editor')
        messages=[]
        sender_bound=False
        for row in await self.page.locator('#imDialog .message .msgBox').all():
            mid=await row.get_attribute('data-index')
            left=await row.locator('.session-item.flex-left').count()
            right=await row.locator('.session-item.flex-right').count()
            if not mid:raise ValueError('message_reference_missing')
            text=row.locator('.text-msg')
            direction=('inbound' if left else 'outbound') if left+right==1 else 'unknown'
            stamp=row.locator('.session-item .info')
            info=await stamp.inner_text() if await stamp.count()==1 else ''
            system_notice=info.strip()=='警告：为了确保交易的安全性和合法性，请勿引导用户线下交易。'
            red_packet_notice=info.strip()==label+'领取了你的红包'
            if system_notice or red_packet_notice:direction='unknown'
            if direction=='inbound':
                sender=re.match(r'^(.*?)\s*(?:今天|昨天|\d{4}/\d{1,2}/\d{1,2})\s+\d{1,2}:\d{2}',info,re.S)
                if not sender or sender.group(1).strip()!=label:
                    (Path(self.profile).parent/'identity-diagnostic.json').write_text(json.dumps({'label':label,'info':info},ensure_ascii=False),'utf-8')
                    raise ValueError('message_sender_does_not_match_selected_customer')
                sender_bound=True
            occurred=rendered_time(info)
            kind='system' if system_notice else 'red_packet' if red_packet_notice else 'unsupported'
            if direction!='unknown':
                if await text.count()==1:kind='text'
                elif await row.locator('.dj-product-simple-card').count()==1:kind='card'
            messages.append({'id':mid,'direction':direction,
                'kind':kind,
                'text':await text.inner_text() if direction!='unknown' and await text.count()==1 else '', 'occurred_at':occurred})
        value=await editor.input_value()
        products=[]
        for card in await self.page.locator('#imDialog .message .dj-product-simple-card').all():
            ids=card.locator('.id-number');titles=card.locator('.sc-right-title')
            if await ids.count()==1 and await titles.count()==1:
                match=re.search(r'\b\d{2,20}\b',await ids.inner_text())
                if match:products.append({'platform':'tongcheng','external_product_id':match.group(),'title':(await titles.inner_text()).strip(),'source':'visible_product_card','reviewed':False})
        if await title.inner_text()!=label or await active.inner_text()!=label:raise ValueError('conversation_changed_during_read')
        if not messages:raise ValueError('message_anchors_unavailable')
        if self.references is None:
            self.references=sqlite3.connect(str(Path(self.profile).parent/'conversation-references.sqlite'))
            self.references.execute('CREATE TABLE IF NOT EXISTS anchors(message TEXT PRIMARY KEY,conversation TEXT NOT NULL)')
            self.references.execute('CREATE TABLE IF NOT EXISTS navigation_hints(conversation TEXT PRIMARY KEY,label TEXT)')
        ref=resolve_anchor_reference(self.references,[m['id'] for m in messages])
        with self.references:self.references.execute('INSERT INTO navigation_hints VALUES(?,?) ON CONFLICT(conversation) DO UPDATE SET label=excluded.label',(ref,label))
        unique_label=await self.page.locator('#imDialog .user-list .user-item h3').filter(has_text=re.compile('^'+re.escape(label)+'$')).count()==1
        sdk_customer=None
        if self.config.get('websocket_bridge'):
            from tongcheng_sdk import ATTACH,DRAIN
            try:
                wire=await self.page.evaluate(ATTACH)
                sdk_customer=wire.pop('current_customer_id',None)
                self.wire_status=wire
                if wire.get('customer_bound') and sdk_customer:self.wire_customers[sdk_customer]=label
                drained=await self.page.evaluate(DRAIN)
                # Incoming wire events accelerate verified navigation. DOM ingestion remains
                # the single reply authority, so wire and rendered copies cannot reply twice.
                for event in drained['events']:
                    if len(self.wire_pending)<256:self.wire_pending.append(event)
                if drained.get('overflow'):self.wire_status['requires_resync']=True
            except Exception:
                self.wire_status={'supported':False,'reason':'sdk_bridge_unavailable'}
        return {'logged_in':True,'identity_verified':True,'send_identity_verified':sender_bound and unique_label,'identity_source':'platform_dom',
            'sdk_customer_id':sdk_customer if self.wire_status.get('customer_bound') else None,
            'conversation_id':ref,'conversation_reference_source':'local_message_anchor_registry',
            'label':label,'messages':messages,'input_text':value,'products':products}

    async def discover_conversations(self):
        # Labels are navigation hints only. Identity is established after switching.
        snap=await self.snapshot()
        if snap.get('input_text'):raise ValueError('human_input_present')
        return await self.page.locator('#imDialog .user-list .user-item h3').all_text_contents()

    async def observe_conversations(self,on_snapshot=None):
        labels=await self.discover_conversations()
        snapshots=[]
        for label in labels:
            before=await self.snapshot()
            if before.get('input_text'):break
            target=self.page.locator('#imDialog .user-list .user-item h3').filter(has_text=re.compile('^'+re.escape(label)+'$'))
            if await target.count()!=1:continue
            await target.click()
            # Require two consistent DOM observations after navigation settles.
            await self.page.wait_for_timeout(350)
            try:
                first=await self.snapshot()
                await self.page.wait_for_timeout(150)
                second=await self.snapshot()
            except ValueError:
                continue
            if first.get('conversation_id')!=second.get('conversation_id') or second.get('label')!=label:continue
            if second.get('input_text'):break
            snapshots.append(second)
            if on_snapshot is not None:on_snapshot(second)
        return snapshots

    async def observe_next(self):
        labels=await self.discover_conversations()
        if not labels:return None
        # Unread badges are scheduling hints, never customer identities.
        priority=[]
        while self.wire_pending:
            event=self.wire_pending.pop(0)
            hint=self.wire_customers.get(event['from'])
            if hint and hint in labels and hint not in priority:priority.append(hint)
        for row in await self.page.locator('#imDialog .user-list .user-item').all():
            badge=row.locator('.badge')
            if await badge.count()==1 and (await badge.inner_text()).strip().isdigit() and int((await badge.inner_text()).strip())>0:
                heading=row.locator('h3')
                if await heading.count()==1:priority.append(await heading.inner_text())
        label=priority[0] if priority else labels[self.scan_index%len(labels)]
        self.scan_index+=1
        target=self.page.locator('#imDialog .user-list .user-item h3').filter(has_text=re.compile('^'+re.escape(label)+'$'))
        if await target.count()!=1:return None
        await target.click()
        for _ in range(12):
            await asyncio.sleep(.15)
            try:
                first=await self.snapshot()
                if first.get('label')!=label:continue
                await asyncio.sleep(.15)
                second=await self.snapshot()
                if second.get('conversation_id')==first.get('conversation_id') and second.get('label')==label:return second
            except ValueError:continue
        return None

    async def locate(self,conversation):
        current=await self.snapshot()
        if current.get('input_text'):raise ValueError('human_input_present')
        if current.get('conversation_id')==conversation:return current
        if self.references:
            hint=self.references.execute('SELECT label FROM navigation_hints WHERE conversation=?',(conversation,)).fetchone()
            if hint:
                target=self.page.locator('#imDialog .user-list .user-item h3').filter(has_text=re.compile('^'+re.escape(hint[0])+'$'))
                if await target.count()==1:
                    await target.click()
                    for _ in range(20):
                        await asyncio.sleep(.15)
                        snap=await self.snapshot()
                        if snap.get('conversation_id')==conversation:return snap
        for snap in await self.observe_conversations():
            if snap.get('conversation_id')==conversation:
                # observe_conversations finishes on another customer; locate again by
                # a navigation hint, then validate message anchors rather than label.
                label=snap['label']
                target=self.page.locator('#imDialog .user-list .user-item h3').filter(has_text=re.compile('^'+re.escape(label)+'$'))
                if await target.count()!=1:raise ValueError('ambiguous_navigation_hint')
                await target.click()
                for _ in range(20):
                    await asyncio.sleep(.15)
                    check=await self.snapshot()
                    if check['conversation_id']==conversation:return check
                break
        raise ValueError('customer_not_located')

    async def send_checked(self, conversation, tail, text):
        before = await self.snapshot()
        if not (before.get('identity_verified') and before['conversation_id'] == conversation
                and before.get('send_identity_verified',True)
                and before['messages'] and before['messages'][-1]['id'] == tail and not before['input_text']):
            raise ValueError('send_target_changed')
        if self.config.get('websocket_bridge') and before.get('sdk_customer_id') and self.wire_status.get('online'):
            from tongcheng_sdk import SEND
            await self.page.evaluate(SEND,{'customer':before['sdk_customer_id'],'text':text,'fingerprint':str(uuid4()),'tail':tail})
            known={m['id'] for m in before['messages']}
            for _ in range(10):
                after=await self.snapshot()
                if after.get('conversation_id')!=conversation:return None
                for message in after['messages']:
                    if message['id'] not in known and message['direction']=='outbound' and message['text']==text:return message['id']
                await asyncio.sleep(.3)
            return None  # Never fall back to a second DOM send after SDK submission.
        await self.page.locator(self.config['editor']).fill(text)
        check = await self.snapshot()
        if not check.get('send_identity_verified',True) or check['conversation_id'] != conversation or check['messages'][-1]['id'] != tail or check['input_text'] != text:
            raise ValueError('send_target_changed_after_fill')
        await self.page.locator(self.config['send']).click(timeout=3000)
        known = {m['id'] for m in before['messages']}
        for _ in range(10):
            after = await self.snapshot()
            if after.get('conversation_id') != conversation:
                return None
            for m in after['messages']:
                if m['id'] not in known and m['direction'] == 'outbound' and m['text'] == text:
                    return m['id']
            await asyncio.sleep(.3)
        return None

    async def close(self):
        if self.context:
            await self.context.close()
            await self.runtime.stop()
        if self.references:
            self.references.close()
            self.references=None
