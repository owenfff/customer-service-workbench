"""Browser-independent authenticated Tongcheng WebSocket transport.

One owner; persistent inbox; no send replay on reconnect. Tokens never leave private storage.
"""
import asyncio
import json
import secrets
import sqlite3
import time
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import httpx
import websockets
from tongcheng_wire import fields,packet,login_body,text_packet
from tongcheng_products import product_card

class DirectTransport:
    def __init__(self,private):
        self.private=Path(private);self.context=None;self.account_verified=False
        self.config={'calibrated':True,'multi_conversation':True,'transport':'independent_websocket'}
        self.wire_status={'transport':'independent_websocket','independent_socket':True,'online':False}
        self.db=sqlite3.connect(self.private/'wire-inbox.sqlite');self.db.row_factory=sqlite3.Row
        self.db.executescript('CREATE TABLE IF NOT EXISTS inbox(customer TEXT,id TEXT,direction TEXT,kind TEXT,text TEXT,at TEXT,PRIMARY KEY(customer,id)); CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,label TEXT,dirty INTEGER DEFAULT 1);')
        self.db.executescript('CREATE TABLE IF NOT EXISTS observations(customer TEXT,id TEXT,received_at TEXT,message_type TEXT,content_type TEXT,content_sample TEXT,PRIMARY KEY(customer,id));')
        self.db.executescript('CREATE TABLE IF NOT EXISTS product_cards(customer TEXT,id TEXT,payload TEXT,PRIMARY KEY(customer,id));')
        self.db.execute("UPDATE inbox SET kind='system' WHERE EXISTS(SELECT 1 FROM observations o WHERE o.customer=inbox.customer AND o.id=inbox.id AND o.message_type='11')")
        # Reclassify observed cards as navigation only; never replay questions.
        for row in list(self.db.execute("SELECT o.*,p.id AS existing_card FROM observations o JOIN inbox i ON i.customer=o.customer AND i.id=o.id LEFT JOIN product_cards p ON p.customer=o.customer AND p.id=o.id WHERE o.message_type='17' AND (p.id IS NULL OR p.payload='[]')")):
            try:products=product_card(json.loads(row['content_sample']))
            except ValueError:continue
            if products is not None and (products or row['existing_card'] is None):
                self.db.execute('INSERT INTO product_cards VALUES(?,?,?) ON CONFLICT(customer,id) DO UPDATE SET payload=excluded.payload',(row['customer'],row['id'],json.dumps(products,ensure_ascii=False)))
                if products:self.db.execute("UPDATE inbox SET kind='card',text=? WHERE customer=? AND id=?",('商品卡：'+products[0]['title'],row['customer'],row['id']))
                self.db.execute('UPDATE customers SET dirty=1 WHERE id=?',(row['customer'],))
        self.db.commit()
        self.socket=None;self.runner=None;self.pending={};self.selected=None;self.last_observed=None;self.closed=False;self.ready=asyncio.Event()
        self.session={};self.started=time.time();self.error=None;self.owner=None;self.live_ids=set()

    def save_session(self):
        path=self.private/'wire-session.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.session,ensure_ascii=False),'utf-8');temp.replace(path)

    async def refresh(self):
        # Same refresh endpoint/shape as the shipped SDK. Never log requests or responses.
        if float(self.session.get('accessTokenExpireInTimestamp',0))-time.time()>3600:return
        token=self.session.get('refreshToken')
        if not token:raise ValueError('reauthentication_required')
        async with httpx.AsyncClient(trust_env=False,timeout=15,follow_redirects=False) as client:
            response=await client.post('https://toca.17u.cn/open-api/uic-idaas-oauth/auth/jwts/refresh',json={'refreshToken':token})
            if response.status_code!=200:raise ValueError('reauthentication_required')
            result=response.json()
        if result.get('code')!=200 or not isinstance(result.get('data'),dict):raise ValueError('reauthentication_required')
        for key in ('accessToken','refreshToken','accessTokenExpireInTimestamp','refreshTokenExpireInTimestamp'):
            if key in result['data']:self.session[key]=result['data'][key]
        self.save_session()

    async def open(self):
        if self.runner and not self.runner.done():return
        path=self.private/'wire-session.json'
        if not path.exists():raise ValueError('initial_login_required')
        if self.owner is None:
            import os
            owner=open(self.private/'wire-owner.lock','a+b');owner.seek(0)
            if not owner.read(1):owner.write(b'1');owner.flush()
            owner.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(owner.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                owner.close();raise ValueError('another_connector_owns_account') from None
            self.owner=owner
        self.session=json.loads(path.read_text('utf-8'))
        self.ready.clear()
        self.closed=False;self.runner=asyncio.create_task(self.run())
        try:await asyncio.wait_for(self.ready.wait(),20)
        except asyncio.TimeoutError:raise ValueError(self.error or 'websocket_connect_timeout') from None

    async def run(self):
        delay=1
        while not self.closed:
            try:
                await self.refresh()
                async with websockets.connect('wss://holidayimws.17u.cn/wss',origin='https://kefu.17u.cn',proxy=None,max_size=1024*1024,ping_interval=20,open_timeout=15) as socket:
                    self.socket=socket
                    sid=secrets.randbelow(2_000_000_000)+1
                    await socket.send(packet(sid,50,login_body(self.session)))
                    first=fields(await asyncio.wait_for(socket.recv(),15))
                    if first.get(1)!=sid or first.get(5)!=20001:raise ValueError('websocket_login_rejected')
                    await socket.send(packet(first[1],52))
                    self.account_verified=True;self.error=None;self.wire_status.update(online=True,login_verified=True,login_verified_at=time.time())
                    self.ready.set();delay=1
                    heartbeat=asyncio.create_task(self.heartbeat())
                    try:
                        async for raw in socket:
                            msg=fields(raw)
                            counts=self.wire_status.setdefault('packet_counts',{})
                            command=str(msg.get(2,'unknown'))
                            if command in counts or len(counts)<32:counts[command]=counts.get(command,0)+1
                            self.wire_status['last_packet_at']=time.time()
                            if msg.get(2)==49:self.wire_status['heartbeat_responses']=self.wire_status.get('heartbeat_responses',0)+1
                            if msg.get(2)!=52:await socket.send(packet(msg.get(1,0),52))
                            waiter=self.pending.get(msg.get(1))
                            if waiter and not waiter.done():waiter.set_result(msg)
                            elif msg.get(2)==46:
                                try:self.receive(msg)
                                except (ValueError,KeyError,TypeError,OverflowError):
                                    self.wire_status['unparsed_events']=self.wire_status.get('unparsed_events',0)+1
                    finally:
                        heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
            except asyncio.CancelledError:break
            except Exception as exc:
                self.error=str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else type(exc).__name__
            finally:
                self.socket=None;self.wire_status.update(online=False,error=self.error)
                for future in self.pending.values():
                    if not future.done():future.set_exception(ConnectionError('connection_lost'))
            if self.error in ('reauthentication_required','websocket_login_rejected'):break
            await asyncio.sleep(delay);delay=min(delay*2,60)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(10)
            await self.socket.send(packet(secrets.randbelow(2_000_000_000)+1,49))
            self.wire_status['heartbeats_sent']=self.wire_status.get('heartbeats_sent',0)+1
            if self.wire_status['heartbeats_sent']%3==0:await self.probe_business()
            if float(self.session.get('accessTokenExpireInTimestamp',0))-time.time()<3600:
                try:await self.refresh()
                except Exception:
                    self.error='reauthentication_required'
                    await self.socket.close();return

    async def probe_business(self):
        """Read-only comparison with the platform session list, never a send API."""
        path=self.private/'business-session.json'
        if not path.exists():return
        checked=datetime.now(timezone.utc).isoformat()
        try:
            auth=json.loads(path.read_text('utf-8'));route='/fenghuoserver/core/session/list'
            async with httpx.AsyncClient(trust_env=False,timeout=12,follow_redirects=False) as client:
                response=await client.post('https://kefu.17u.cn/fenghuoweb/api/proxy'+route,
                    headers={'token':auth['token'],'x-proxy-path':route,'x-platform':'pc','x-account':auth['account']},
                    json={'tenantCode':self.session['tenant_code'],'imUserId':self.session['source_uid'],'beginTime':'','targetUserId':''})
                response.raise_for_status();data=response.json()
            if data.get('code')==20000 and isinstance(data.get('data'),dict) and data['data'].get('token'):
                auth['token']=data['data']['token'];temp=path.with_suffix('.tmp');temp.write_text(json.dumps(auth),'utf-8');temp.replace(path)
                self.wire_status['business_probe']={'checked_at':checked,'state':'token_refreshed'};return
            if data.get('code')!=10000 or not isinstance(data.get('data'),list):raise ValueError('business_auth_or_response_invalid')
            rows=data['data'];latest=max((r.get('msgTime2',0) or 0 for r in rows),default=0)
            gaps=[]
            for row in rows:
                if not row.get('lastMsgFingerprint') or (row.get('msgTime2',0) or 0)/1000<self.started:continue
                if str(row.get('lastMsgSrcUserUid'))!=str(row.get('srcUserUid')) or str(row.get('msgType'))!='0':continue
                if not self.db.execute('SELECT 1 FROM inbox WHERE customer=? AND id=?',(str(row['srcUserUid']),row['lastMsgFingerprint'])).fetchone():gaps.append(row['lastMsgFingerprint'])
            self.wire_status['business_probe']={'checked_at':checked,'state':'socket_gap_detected' if gaps else 'compared',
                'session_count':len(rows),'latest_session_activity_at':datetime.fromtimestamp(latest/1000,timezone.utc).isoformat() if latest else None,'unseen_latest_texts':len(gaps)}
        except Exception as exc:
            self.wire_status['business_probe']={'checked_at':checked,'state':'check_failed','error':str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else type(exc).__name__}

    def receive(self,envelope):
        payload=json.loads(fields(envelope.get(3,b''))[1].decode())
        protocols=self.wire_status.setdefault('protocol_counts',{})
        protocol=str(payload.get('typeu','unknown'))[:40]
        if protocol in protocols or len(protocols)<32:protocols[protocol]=protocols.get(protocol,0)+1
        if payload.get('type') in ('s_evt.duplicated','s_evt.illegal'):
            self.error='websocket_login_rejected'
            self.wire_status['online']=False
            if self.socket:asyncio.create_task(self.socket.close())
            return
        if str(payload.get('typeu'))!='3':return
        source,target=str(payload.get('from','')),str(payload.get('to',''))
        me=str(self.session['source_uid'])
        if me not in (source,target) or source==target:
            self.wire_status['recipient_mismatches']=self.wire_status.get('recipient_mismatches',0)+1
            return
        customer=source if target==me else target
        mid=payload.get('fp')
        if not mid or not customer:return
        body=json.loads(payload['dataContent'])
        message_type=str(body.get('ty'))
        received_at=datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)',(customer,mid,received_at,message_type,type(body.get('m')).__name__,json.dumps(body.get('m'),ensure_ascii=False)[:8000]))
        # Receipts, typing, revocation and system notifications are not consultations.
        if message_type in ('11','90','91','92','93','990'):
            self.wire_status['control_events']=self.wire_status.get('control_events',0)+1
            return
        kind='text' if message_type=='0' and isinstance(body.get('m'),str) and body['m'].strip() else 'unsupported'
        products=product_card(body.get('m')) if message_type=='17' else None
        if products:kind='card'
        timestamp=envelope.get(4,0)
        if timestamp>10**12:timestamp/=1000
        at=datetime.fromtimestamp(timestamp,timezone.utc).isoformat() if 0<timestamp<time.time()+86400 else None
        label=body.get('nickname') if target==me else None
        if not isinstance(label,str) or not label:label='客户 '+customer
        with self.db:
            text=body.get('m') if kind=='text' else '商品卡：'+products[0]['title'] if products else ''
            inserted=self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?)',(customer,mid,'inbound' if target==me else 'outbound',kind,text,at)).rowcount
            if products is not None:self.db.execute('INSERT OR IGNORE INTO product_cards VALUES(?,?,?)',(customer,mid,json.dumps(products,ensure_ascii=False)))
            if inserted:self.db.execute('INSERT INTO customers VALUES(?,?,1) ON CONFLICT(id) DO UPDATE SET dirty=1',(customer,label))
        if inserted:self.live_ids.add((customer,mid))
        if inserted:
            counter='text_messages' if kind=='text' else 'product_cards' if kind=='card' else 'unsupported_events'
            self.wire_status[counter]=self.wire_status.get(counter,0)+1
            if target==me:
                self.wire_status['last_customer_event_at']=received_at
                if kind=='text':self.wire_status['last_customer_text_at']=received_at
        self.wire_status['received_messages']=self.db.execute('SELECT count(*) FROM inbox').fetchone()[0]

    def customer_snapshot(self,customer):
        rows=list(self.db.execute("SELECT * FROM (SELECT rowid,* FROM inbox WHERE customer=? AND kind!='system' ORDER BY rowid DESC LIMIT 200) ORDER BY rowid",(customer,))) if customer else []
        name=self.db.execute('SELECT label FROM customers WHERE id=?',(customer,)).fetchone() if customer else None
        cards={r['id']:json.loads(r['payload']) for r in self.db.execute('SELECT * FROM product_cards WHERE customer=?',(customer,))} if customer else {}
        observed={r['id']:dict(r) for r in self.db.execute('SELECT id,received_at,message_type,content_sample FROM observations WHERE customer=?',(customer,))}
        from tongcheng_ownership import classify_outbound
        origins={r['id']:classify_outbound(r['kind'],r['text'],observed.get(r['id'],{}).get('message_type'),observed.get(r['id'],{}).get('content_sample')) for r in rows if r['direction']=='outbound'}
        latest=self.db.execute('SELECT p.payload FROM product_cards p JOIN inbox i ON i.customer=p.customer AND i.id=p.id WHERE p.customer=? ORDER BY i.rowid DESC LIMIT 1',(customer,)).fetchone() if customer else None
        return {'logged_in':bool(self.wire_status.get('online')),'identity_verified':self.account_verified,'send_identity_verified':bool(customer and rows),
            'identity_source':'platform_websocket','conversation_id':'ws:'+customer if customer else 'ws:idle','conversation_reference_source':'platform_websocket_uid',
            'message_id_source':'platform_websocket_fingerprint','label':name[0] if name else '等待新咨询','input_text':'','products':json.loads(latest[0]) if latest else [],
            'messages':[{'id':r['id'],'direction':r['direction'],'kind':r['kind'],'text':r['text'],'occurred_at':r['at'],'received_at':observed.get(r['id'],{}).get('received_at'),'outbound_origin':origins.get(r['id']),'products':cards.get(r['id'],[]),'newly_received':(customer,r['id']) in self.live_ids} for r in rows]}

    async def snapshot(self):return self.customer_snapshot(self.selected)
    async def observe_next(self):
        if self.last_observed:
            customer,tail=self.last_observed
            current=self.db.execute("SELECT id FROM inbox WHERE customer=? AND kind!='system' ORDER BY rowid DESC LIMIT 1",(customer,)).fetchone()
            if current and current[0]==tail:
                with self.db:self.db.execute('UPDATE customers SET dirty=0 WHERE id=?',(customer,))
            self.last_observed=None
        row=self.db.execute('SELECT id FROM customers WHERE dirty=1 ORDER BY rowid LIMIT 1').fetchone()
        if not row:return None
        self.selected=row[0];snap=await self.snapshot()
        if not snap['messages']:
            with self.db:self.db.execute('UPDATE customers SET dirty=0 WHERE id=?',(self.selected,))
            return None
        if snap['messages']:self.last_observed=(self.selected,snap['messages'][-1]['id'])
        return snap
    async def locate(self,conversation):
        if not conversation.startswith('ws:'):raise ValueError('legacy_browser_conversation_requires_new_message')
        customer=conversation[3:]
        if not self.db.execute('SELECT 1 FROM customers WHERE id=?',(customer,)).fetchone():raise ValueError('unknown_customer')
        self.selected=customer;return await self.snapshot()
    async def send_checked(self,conversation,tail,text):
        snap=await self.locate(conversation)
        from tongcheng_ownership import relevant_messages
        snap['messages']=relevant_messages(snap['messages'])
        if not snap['logged_in'] or not snap['messages'] or snap['messages'][-1]['id']!=tail:raise ValueError('send_target_changed')
        sid=secrets.randbelow(2_000_000_000)+1;fp=str(uuid4())
        future=asyncio.get_running_loop().create_future();self.pending[sid]=future
        try:
            await self.socket.send(packet(sid,46,text_packet(self.session,self.selected,text,fp)))
            ack=await asyncio.wait_for(future,10)
            if ack.get(5)!=20001:return None
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?)',(self.selected,fp,'outbound','text',text,datetime.now(timezone.utc).isoformat()))
            # A protocol success response is server acceptance, not page echo or delivery.
            return {'id':fp,'kind':'server_accepted'}
        finally:self.pending.pop(sid,None)
    async def close(self):
        self.closed=True
        if self.runner:self.runner.cancel();await asyncio.gather(self.runner,return_exceptions=True)
        self.db.close()
        if self.owner:self.owner.close();self.owner=None
