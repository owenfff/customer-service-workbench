"""Tongcheng local connector. Browser transport is explicitly calibrated, never guessed."""
from __future__ import annotations
import asyncio
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from contracts import AccountHealth, CanonicalEvent, DeliveryReceipt


class TongchengService:
    platform = 'tongcheng'

    def __init__(self, path, transport=None, knowledge=lambda: ()):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS messages(account TEXT, conversation TEXT, id TEXT,
          direction TEXT, text TEXT, kind TEXT, PRIMARY KEY(account,conversation,id));
        CREATE TABLE IF NOT EXISTS message_origins(account TEXT,conversation TEXT,id TEXT,origin TEXT,PRIMARY KEY(account,conversation,id));
        CREATE TABLE IF NOT EXISTS sessions(account TEXT, conversation TEXT, human INTEGER DEFAULT 0,
          PRIMARY KEY(account,conversation));
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, account TEXT, conversation TEXT,
          message TEXT, text TEXT, status TEXT, reason TEXT,
          UNIQUE(account,conversation,message));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT,action TEXT,entity TEXT);
        CREATE TABLE IF NOT EXISTS policies(account TEXT PRIMARY KEY,mode TEXT,conversations TEXT);
        CREATE TABLE IF NOT EXISTS conversation_meta(account TEXT,conversation TEXT,label TEXT,reference_source TEXT,
          PRIMARY KEY(account,conversation));
        CREATE TABLE IF NOT EXISTS product_context(account TEXT,conversation TEXT,payload TEXT,
          PRIMARY KEY(account,conversation));
        CREATE TABLE IF NOT EXISTS document_reviews(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS handoff_cases(task TEXT PRIMARY KEY,account TEXT,conversation TEXT,reason TEXT,status TEXT,created_at TEXT,notified_at TEXT);
        CREATE TABLE IF NOT EXISTS simulations(id INTEGER PRIMARY KEY,task TEXT,account TEXT,conversation TEXT,at TEXT,status TEXT,text TEXT,evidence TEXT);
        ''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}
        if 'context' not in {r[1] for r in self.db.execute('PRAGMA table_info(simulations)')}:
            self.db.execute("ALTER TABLE simulations ADD COLUMN context TEXT DEFAULT '{}'")
        for name,definition in [('source',"TEXT DEFAULT 'reviewed_source_text'"),('evidence',"TEXT DEFAULT '[]'"),('fingerprint','TEXT'),('knowledge_query','TEXT'),('snapshot_anchor','TEXT')]:
            if name not in columns:self.db.execute('ALTER TABLE tasks ADD COLUMN '+name+' '+definition)
        # A crash after claiming a send is uncertain and must never be retried.
        self.db.execute("UPDATE tasks SET status='uncertain',reason='restart_during_send' WHERE status='sending'")
        if 'details' not in {r[1] for r in self.db.execute('PRAGMA table_info(audit)')}:
            self.db.execute("ALTER TABLE audit ADD COLUMN details TEXT DEFAULT '{}'")
        self.db.execute("UPDATE policies SET mode='draft' WHERE mode IN ('test_auto','auto')")
        self.db.commit()
        self.transport, self.knowledge = transport, knowledge
        self.active = False
        self.auto = False
        self.baselines = set()
        self.lock = asyncio.Lock()
        self.state = 'paused'
        self.last_error=None
        self.started_at=datetime.now(timezone.utc)
        self.initial_scan_done=False
        self.knowledge_scopes=lambda:{}
        self.shadow_documents=lambda:[]
        self.model_available=lambda:False
        self.production_authorized=lambda:False
        self.production_revision=lambda:None
        self.authorized_library=lambda:[]

    def dialogue_history(self,account,conversation,through=None):
        query="""SELECT m.* FROM messages m WHERE m.account=? AND m.conversation=? AND m.kind!='system'
          AND NOT EXISTS(SELECT 1 FROM message_origins o WHERE o.account=m.account AND o.conversation=m.conversation AND o.id=m.id
          AND o.origin IN ('automatic_welcome','automatic_away','platform_question_card','coupon_card'))"""
        args=[account,conversation]
        if through:
            query+=' AND m.rowid<=(SELECT rowid FROM messages WHERE account=? AND conversation=? AND id=?)'
            args.extend([account,conversation,through])
        return [dict(r) for r in self.db.execute(query+' ORDER BY m.rowid DESC LIMIT 20',args)][::-1]

    def knowledge_for(self,account,conversation):
        row=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(account,conversation)).fetchone()
        products=json.loads(row[0]) if row else []
        current=products[-1].get('external_product_id') if products else None
        scopes=self.knowledge_scopes()
        return [f for f in self.knowledge() if not scopes.get(f.id) or current in scopes[f.id]]

    def audit(self,action,entity,**details):
        # Metadata only: bodies remain in the existing private message/task store.
        allowed={'model','status','reason','duration_ms','source','conversation','message_id','reply_length','reference_count','mode','exception_type'}
        safe={k:v for k,v in details.items() if k in allowed and isinstance(v,(str,int,bool))}
        safe={k:(v[:160] if isinstance(v,str) else v) for k,v in safe.items()}
        self.db.execute('INSERT INTO audit(at,action,entity,details) VALUES(?,?,?,?)',(datetime.now(timezone.utc).isoformat(),action,entity,json.dumps(safe,ensure_ascii=False)))

    def disable_auto(self,account):
        changed=self.db.execute("UPDATE policies SET mode='draft' WHERE account=? AND mode IN ('test_auto','auto')",(account,)).rowcount
        if changed:self.audit('auto_paused_connection_lost',account)
        self.db.commit()

    def policy(self,account,mode,conversations=()):
        if mode not in ('readonly','draft','test_auto','auto','shadow'):raise ValueError('invalid_mode')
        if mode=='auto':
            if not self.model_available():raise ValueError('model_call_not_verified')
            if not self.approved_documents() and not any(f.reviewed and f.enabled and f.valid_from<=datetime.now(timezone.utc)<f.expires_at for f in self.knowledge()):raise ValueError('reviewed_knowledge_required')
            if not self.production_authorized() and not self.db.execute("SELECT 1 FROM tasks WHERE account=? AND status='customer_confirmed' LIMIT 1",(account,)).fetchone():raise ValueError('real_send_acceptance_required')
        if mode=='test_auto':
            passed={r[0] for r in self.db.execute("SELECT DISTINCT conversation FROM tasks WHERE account=? AND status IN ('echo_observed','customer_confirmed')",(account,))}
            if not conversations or not set(conversations)<=passed:raise ValueError('test_conversations_require_verified_manual_echo')
        if mode in ('auto','test_auto'):
            self.db.execute("UPDATE tasks SET status='cancelled',reason='auto_enabled_future_messages_only' WHERE account=? AND status IN ('pending','human_review')",(account,))
        self.db.execute('INSERT INTO policies VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET mode=excluded.mode,conversations=excluded.conversations',(account,mode,json.dumps(list(conversations))))
        self.audit('policy_'+mode,account);self.db.commit()

    async def connect(self, account_id):
        if self.transport is None:
            self.state = 'error'
            return AccountHealth('error', 'browser_not_calibrated')
        self.active = True
        self.baselines.clear()
        self.started_at=datetime.now(timezone.utc)
        self.initial_scan_done=False
        return await self.health(account_id)

    async def disconnect(self, account_id):
        self.active = False
        self.auto = False
        self.state = 'paused'
        self.db.execute("UPDATE tasks SET status='cancelled',reason='account_paused' WHERE account=? AND status='pending'",(account_id,))
        self.db.commit()

    async def health(self, account_id):
        if not self.active:
            return AccountHealth(self.state, 'real_send_unverified')
        try:
            async with self.lock:snap = await self.transport.snapshot()
            self.state = ('connected' if snap.get('identity_verified') else 'error') if snap.get('logged_in') else 'needs_login'
            if self.state=='connected':self.last_error=None
            if self.state != 'connected':
                self.auto = False
                self.disable_auto(account_id)
            return AccountHealth(self.state)
        except Exception:
            self.state = 'error'
            self.auto = False
            self.disable_auto(account_id)
            return AccountHealth('error', 'browser_unavailable')

    def handoff(self, account, conversation, human):
        self.db.execute('INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(account,conversation) DO UPDATE SET human=excluded.human', (account,conversation,int(human)))
        self.db.execute("UPDATE tasks SET status='cancelled',reason='operator_control_changed' WHERE account=? AND conversation=? AND status IN ('pending','human_review')",(account,conversation))
        self.audit('human_takeover' if human else 'return_to_ai',conversation)
        self.db.execute("UPDATE handoff_cases SET status=? WHERE account=? AND conversation=? AND status IN ('open','in_progress')",('in_progress' if human else 'closed',account,conversation))
        self.db.commit()

    def prepare_handoff_notice(self,task_id,reason):
        """Persist a real queue item before acknowledging it; never claim resolution."""
        task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task or task['status'] not in ('pending','human_review') or task['source']=='manual':return False
        policy=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone()
        if not policy or policy[0]!='auto' or self.is_human(task['account'],task['conversation']):return False
        now=datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT OR IGNORE INTO handoff_cases VALUES(?,?,?,?,'open',?,NULL)",(task_id,task['account'],task['conversation'],reason,now))
        self.audit('human_queue_recorded',task_id,reason=reason)
        # The user requested a silent human queue: never send a handoff notice.
        self.db.commit();return False

    def is_human(self, account, conversation):
        row = self.db.execute('SELECT human FROM sessions WHERE account=? AND conversation=?', (account,conversation)).fetchone()
        return bool(row and row[0])

    def ingest(self, account, snap):
        try:
            return self._ingest(account, snap)
        except Exception:
            self.db.rollback()
            raise

    def _ingest(self, account, snap):
        if not snap.get('logged_in') or not snap.get('identity_verified'):
            raise ValueError('identity_or_login_unverified')
        conversation = snap['conversation_id']
        if snap.get('identity_source') not in ('platform_dom','platform_websocket'):
            raise ValueError('stable_dom_identity_required')
        key = (account, conversation)
        self.db.execute('INSERT INTO conversation_meta VALUES(?,?,?,?) ON CONFLICT(account,conversation) DO UPDATE SET label=excluded.label',
            (account,conversation,snap.get('label',conversation),snap.get('conversation_reference_source','platform_dom_attribute')))
        baseline = key not in self.baselines
        mode=self.db.execute('SELECT mode FROM policies WHERE account=?',(account,)).fetchone()
        if 'products' in snap:
            self.db.execute('INSERT INTO product_context VALUES(?,?,?) ON CONFLICT(account,conversation) DO UPDATE SET payload=excluded.payload',(account,conversation,json.dumps(snap['products'],ensure_ascii=False)))
        events = []
        for index,msg in enumerate(snap['messages']):
            from tongcheng_shadow_context import following_question
            from tongcheng_ownership import relevant_messages
            prior=relevant_messages(snap['messages'][:index])
            previous=prior[-1] if prior else None
            enrich=bool(mode and mode[0] not in ('readonly',) and following_question(previous,msg))
            if not msg.get('id') or msg['direction'] not in ('inbound','outbound','unknown'):
                raise ValueError('message_identity_unverified')
            inserted = self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',
                (account,conversation,msg['id'],msg['direction'],msg.get('text',''),msg.get('kind','unsupported'))).rowcount
            if not inserted and msg.get('kind')=='card':
                self.db.execute("UPDATE messages SET kind='card' WHERE account=? AND conversation=? AND id=? AND kind='unsupported'",(account,conversation,msg['id']))
            from tongcheng_ownership import classify_outbound,passive
            if msg['direction']=='outbound':
                origin=msg.get('outbound_origin') or classify_outbound(msg.get('kind'),msg.get('text',''))
                msg=dict(msg,outbound_origin=origin)
                self.db.execute('INSERT OR REPLACE INTO message_origins VALUES(?,?,?,?)',(account,conversation,msg['id'],origin))
            new_consultation=False
            if baseline and snap.get('identity_source')=='platform_websocket' and mode and mode[0] in ('shadow','auto','test_auto') and msg.get('newly_received'):
                new_consultation=True
            if baseline and (self.initial_scan_done or snap.get('identity_source')=='platform_websocket') and msg.get('occurred_at'):
                try:new_consultation=new_consultation or datetime.fromisoformat(msg['occurred_at'])>self.started_at
                except (ValueError,TypeError):pass
            if not inserted or (baseline and not new_consultation):
                continue
            if passive(msg):
                self.audit('platform_message_not_human_takeover',msg['id'],conversation=conversation,reason=msg['outbound_origin'])
                continue
            # Shadow analysis can run alongside a human reply; it never owns sending.
            # A subsequent customer question still invalidates the older candidate.
            if not enrich and not (msg['direction']=='outbound' and mode and mode[0]=='shadow'):
                self.db.execute("UPDATE tasks SET status='cancelled',reason='new_message_observed' WHERE account=? AND conversation=? AND status IN ('pending','human_review')",(account,conversation))
            if msg['direction']=='unknown':
                self.audit('unclassified_message_requires_platform_review',conversation)
                continue
            if msg['direction'] == 'outbound':
                self.db.execute('INSERT INTO sessions VALUES(?,?,1) ON CONFLICT(account,conversation) DO UPDATE SET human=1', (account,conversation))
                self.audit('merchant_reply_human_takeover',conversation,message_id=msg['id'])
                self.db.execute("UPDATE handoff_cases SET status='in_progress' WHERE account=? AND conversation=? AND status='open'",(account,conversation))
                continue
            if msg.get('kind')=='card' and msg.get('products'):
                self.audit('product_card_context_observed',conversation)
                if enrich:
                    pending=self.db.execute("SELECT id FROM tasks WHERE account=? AND conversation=? AND message=? AND status IN ('pending','human_review')",(account,conversation,previous['id'])).fetchone()
                    if pending:
                        # Requeue the original question, not the navigation card.
                        scoped=hashlib.sha256(json.dumps([account,conversation,previous['id'],msg['id']]).encode()).hexdigest()
                        events.append(CanonicalEvent('tongcheng:'+scoped,self.platform,account,conversation,previous['id'],
                            'inbound',conversation,{'type':'text','text':previous.get('text','')},datetime.now(timezone.utc),
                            conversation_id_source=snap.get('conversation_reference_source','platform_dom_attribute'),message_id_source=snap.get('message_id_source','dom_reference')))
                        self.audit('shadow_question_enriched_by_following_card',pending[0])
                continue
            from qianniu_knowledge import retrieve_faqs
            if msg.get('kind')=='text':
                self.db.execute("UPDATE handoff_cases SET status='superseded' WHERE account=? AND conversation=? AND status='open'",(account,conversation))
            result = retrieve_faqs(msg.get('text',''), self.knowledge_for(account,conversation),
                kind='display_text' if msg.get('kind') == 'text' else 'unsupported', now=datetime.now(timezone.utc))
            blocked = self.is_human(account,conversation)
            draft = result.matches[0].answer if result.status == 'matched' and not blocked else ''
            # Dynamic price/inventory/commitments require a human even on FAQ match.
            if any(word in msg.get('text','') for word in ('价格','库存','余位','退款','优惠','多少钱')):
                draft = ''
            self.db.execute('INSERT OR IGNORE INTO tasks(id,account,conversation,message,text,status,reason) VALUES(?,?,?,?,?,?,?)',
                (uuid4().hex,account,conversation,msg['id'],draft,'pending' if draft else 'human_review',
                 'reviewed_source_text' if draft else 'human_or_knowledge_required'))
            if msg.get('kind')=='text':
                received_task=self.db.execute('SELECT id FROM tasks WHERE account=? AND conversation=? AND message=?',(account,conversation,msg['id'])).fetchone()
                self.audit('customer_message_received',received_task[0],conversation=conversation,message_id=msg['id'],mode=mode[0] if mode else 'draft')
            scoped=hashlib.sha256(json.dumps([account,conversation,msg['id']]).encode()).hexdigest()
            events.append(CanonicalEvent('tongcheng:'+scoped,self.platform,account,conversation,msg['id'],
                'inbound',conversation,{'type':msg.get('kind','unsupported'),'text':msg.get('text','')},datetime.now(timezone.utc),
                conversation_id_source=snap.get('conversation_reference_source','platform_dom_attribute'),message_id_source=snap.get('message_id_source','dom_reference')))
        self.db.commit()
        self.baselines.add(key)
        return events

    async def listen(self, account_id, cursor=None):
        while self.active:
            try:
                if (await self.health(account_id)).status != 'connected':
                    await asyncio.sleep(2)
                    continue
                async with self.lock:
                    events=[]
                    if hasattr(self.transport,'observe_next') and getattr(self.transport,'config',{}).get('multi_conversation'):
                        snap=await self.transport.observe_next()
                        if snap:events=self.ingest(account_id,snap)
                    elif hasattr(self.transport,'observe_conversations') and getattr(self.transport,'config',{}).get('multi_conversation'):
                        await self.transport.observe_conversations(lambda snapshot:events.extend(self.ingest(account_id,snapshot)))
                    else:
                        events=self.ingest(account_id,await self.transport.snapshot())
                    self.initial_scan_done=True
                for event in events:
                    yield event
            except Exception as exc:
                self.state = 'error'
                self.auto = False
                self.disable_auto(account_id)
                self.last_error=str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else type(exc).__name__
            await asyncio.sleep(2)

    async def send_message(self, command):
        async with self.lock:
            task = self.db.execute('SELECT * FROM tasks WHERE id=?', (command.idempotency_key,)).fetchone()
            if not task or task['status'] != 'pending' or not self.active or not self.transport:
                return DeliveryReceipt(False,None,'failed','task_not_sendable')
            if (task['account'],task['conversation'],task['text']) != (command.account_id,command.conversation_id,command.content.get('text')):
                return DeliveryReceipt(False,None,'failed','command_mismatch')
            if self.is_human(task['account'],task['conversation']) and task['source']!='manual':
                return DeliveryReceipt(False,None,'failed','human_takeover')
            policy=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone()
            if policy and policy[0]=='readonly':return DeliveryReceipt(False,None,'failed','readonly_account')
            if policy and policy[0]=='shadow':return DeliveryReceipt(False,None,'failed','shadow_mode_no_send')
            if policy and policy[0]=='auto' and not self.production_authorized():return DeliveryReceipt(False,None,'failed','production_authorization_invalid')
            if policy and policy[0]=='auto' and not self.model_available():return DeliveryReceipt(False,None,'failed','model_call_not_verified')
            from qianniu_knowledge import retrieve_faqs
            source = self.db.execute('SELECT text FROM messages WHERE account=? AND conversation=? AND id=?',
                (task['account'],task['conversation'],task['message'])).fetchone()
            current = retrieve_faqs(task['knowledge_query'] or (source[0] if source else ''),self.knowledge_for(task['account'],task['conversation']),now=datetime.now(timezone.utc))
            if task['source']=='documents_model':
                from tongcheng_document_review import fingerprint
                row=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
                product=json.loads(row[0]) if row else []
                if fingerprint(product,json.loads(task['evidence']),self.approved_documents())!=task['fingerprint']:return DeliveryReceipt(False,None,'failed','knowledge_changed_or_expired')
                if not self.model_available():return DeliveryReceipt(False,None,'failed','model_call_not_verified')
            if task['source'] in ('clarification_model','clarification_prompt'):
                from tongcheng_production import clarification_slot,clarification_fingerprint
                history=self.dialogue_history(task['account'],task['conversation'])
                row=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
                products=json.loads(row[0]) if row else []
                slot=clarification_slot(source[0] if source else '',history,task['text'])
                if not slot or slot!=task['knowledge_query'] or not self.production_authorized() or not self.model_available() or clarification_fingerprint(products,self.production_revision(),slot,task['text'])!=task['fingerprint']:return DeliveryReceipt(False,None,'failed','clarification_no_longer_valid')
            if task['source']=='handoff_notice':
                return DeliveryReceipt(False,None,'failed','handoff_notice_disabled')
            if task['source'] not in ('manual','documents_model','clarification_model','clarification_prompt','handoff_notice') and (current.status != 'matched' or current.matches[0].answer != task['text']):
                return DeliveryReceipt(False,None,'failed','knowledge_changed_or_expired')
            if task['source']=='model':
                fingerprint=hashlib.sha256(json.dumps(current.matches[0].model_dump(mode='json'),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if fingerprint!=task['fingerprint']:return DeliveryReceipt(False,None,'failed','knowledge_changed')
            try:
                if hasattr(self.transport,'locate'):await self.transport.locate(task['conversation'])
                snap = await self.transport.snapshot()
                from tongcheng_ownership import relevant_messages
                snap=dict(snap,messages=relevant_messages(snap['messages']))
                if not (snap.get('logged_in') and snap.get('identity_verified') and snap.get('identity_source') in ('platform_dom','platform_websocket')
                        and snap.get('send_identity_verified',True)
                        and snap['conversation_id'] == task['conversation'] and not snap.get('input_text')
                        and snap['messages'] and snap['messages'][-1]['id'] == (task['snapshot_anchor'] or task['message'])):
                    return DeliveryReceipt(False,None,'failed','conversation_changed_or_input_not_empty')
            except Exception:
                return DeliveryReceipt(False,None,'failed','preflight_failed')
            self.db.execute("UPDATE tasks SET status='sending' WHERE id=?", (task['id'],))
            self.audit('send_attempted',task['id'],source=task['source'],reply_length=len(task['text']))
            self.db.commit()
            status = 'uncertain'
            try:
                # Transport rechecks identity and tail immediately before filling/clicking.
                echoed = await self.transport.send_checked(task['conversation'],task['snapshot_anchor'] or task['message'],task['text'])
                if echoed:
                    if isinstance(echoed,dict) and echoed.get('kind')=='server_accepted':
                        self.db.execute("UPDATE tasks SET status='server_accepted',reason='websocket_protocol_acceptance_only' WHERE id=?",(task['id'],))
                        self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',(task['account'],task['conversation'],echoed['id'],'outbound',task['text'],'text'))
                        self.audit('server_accepted',task['id']);self.db.commit()
                        if task['source']=='handoff_notice':
                            self.db.execute('UPDATE handoff_cases SET notified_at=? WHERE task=?',(datetime.now(timezone.utc).isoformat(),task['id']));self.db.commit()
                        return DeliveryReceipt(True,echoed['id'],'pending','server_accepted')
                    self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',
                        (task['account'],task['conversation'],echoed,'outbound',task['text'],'text'))
                    status = 'echo_observed'
            except asyncio.CancelledError:
                self.db.execute("UPDATE tasks SET status='uncertain',reason='send_interrupted' WHERE id=?",(task['id'],))
                self.db.commit()
                raise
            except Exception:
                pass
            self.db.execute('UPDATE tasks SET status=?,reason=? WHERE id=?', (status,'page_echo_only',task['id']))
            self.audit(status,task['id'])
            if status=='echo_observed' and task['source']=='handoff_notice':self.db.execute('UPDATE handoff_cases SET notified_at=? WHERE task=?',(datetime.now(timezone.utc).isoformat(),task['id']))
            self.db.commit()
            return DeliveryReceipt(status == 'echo_observed',None,'pending',status)

    def edit_task(self,task_id,text):
        if not text.strip() or len(text)>8000:raise ValueError('invalid_reply')
        task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task or task['status'] not in ('pending','human_review'):raise ValueError('task_not_editable')
        self.db.execute("UPDATE tasks SET text=?,source='manual',evidence='[]',fingerprint=NULL,status='pending',reason='operator_edited' WHERE id=?",(text,task_id))
        self.audit('manual_draft_saved',task_id)
        self.db.commit()

    def manual_reply(self,account,conversation,text,expected_message=None):
        if not text.strip() or len(text)>8000:raise ValueError('invalid_reply')
        tail=self.db.execute('SELECT id FROM messages WHERE account=? AND conversation=? ORDER BY rowid DESC LIMIT 1',(account,conversation)).fetchone()
        if not tail:raise ValueError('conversation_not_observed')
        if expected_message is not None and tail[0]!=expected_message:raise ValueError('conversation_changed')
        existing=self.db.execute('SELECT * FROM tasks WHERE account=? AND conversation=? AND message=?',(account,conversation,tail[0])).fetchone()
        if existing:
            self.edit_task(existing['id'],text);return existing['id']
        task_id=uuid4().hex
        self.db.execute("INSERT INTO tasks(id,account,conversation,message,text,status,reason,source) VALUES(?,?,?,?,?,'pending','operator_edited','manual')",(task_id,account,conversation,tail[0],text))
        self.audit('manual_draft_saved',task_id);self.db.commit()
        return task_id

    def record_simulation(self,task_id,status):
        task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task:return
        mode=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone()
        if not mode or mode[0]!='shadow':return
        valid=status=='draft_ready' and task['status']=='pending' and task['source']=='model'
        self.db.execute('INSERT INTO simulations(task,account,conversation,at,status,text,evidence) VALUES(?,?,?,?,?,?,?)',
            (task_id,task['account'],task['conversation'],datetime.now(timezone.utc).isoformat(),status if task['status']!='cancelled' else 'superseded',task['text'] if valid else '',task['evidence'] if valid else '[]'))
        self.audit('shadow_generation_only',task_id);self.db.commit()

    async def generate_task(self,task_id,config,provider=None):
        try:
            async with self.lock:
                task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
                mode=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone() if task else None
                use_documents=bool(mode and mode[0]=='shadow' and self.shadow_documents())
            if mode and mode[0] not in ('shadow','readonly') and self.approved_documents():return await self._generate_approved_documents(task_id,config,provider)
            if use_documents:
                result=await self._generate_shadow_documents(task_id,config,provider)
                if result['status']!='no_source_match':return result
            result=await self._generate_task(task_id,config,provider)
        except Exception as exc:
            async with self.lock:self.record_simulation(task_id,str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else 'model_error')
            raise
        async with self.lock:self.record_simulation(task_id,result['status'])
        return result

    async def _generate_shadow_documents(self,task_id,config,provider=None):
        from tongcheng_ownership import customer_question as merged_question
        from workbench_core.shadow_documents import shadow_draft
        async with self.lock:
            task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['status'] not in ('pending','human_review'):raise ValueError('task_not_editable')
            # This path writes simulations only. Human ownership remains in force.
            mode=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone()
            if not mode or mode[0]!='shadow':raise ValueError('shadow_mode_required')
            history=self.dialogue_history(task['account'],task['conversation'],task['message'])
            if not history or history[-1]['id']!=task['message']:raise ValueError('conversation_changed')
            question=merged_question(history)
            if question is None:raise ValueError('unsupported_message')
            product=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            products=json.loads(product[0]) if product else []
            inbound_version=self.db.execute("SELECT id FROM messages WHERE account=? AND conversation=? AND direction='inbound' AND kind!='system' AND NOT EXISTS(SELECT 1 FROM message_origins o WHERE o.account=messages.account AND o.conversation=messages.conversation AND o.id=messages.id AND o.origin IN ('automatic_welcome','automatic_away','platform_question_card','coupon_card')) ORDER BY rowid DESC LIMIT 1",(task['account'],task['conversation'])).fetchone()
            documents=self.shadow_documents()
            self.audit('shadow_model_requested',task_id);self.db.commit()
        result=await asyncio.to_thread(shadow_draft,question,history,documents,config,product=products,provider=provider,allow_clarification=True)
        if result['status']=='no_source_match':return result
        async with self.lock:
            fresh=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            mode=self.db.execute('SELECT mode FROM policies WHERE account=?',(task['account'],)).fetchone()
            tail=self.db.execute("SELECT id FROM messages WHERE account=? AND conversation=? AND direction='inbound' AND kind!='system' AND NOT EXISTS(SELECT 1 FROM message_origins o WHERE o.account=messages.account AND o.conversation=messages.conversation AND o.id=messages.id AND o.origin IN ('automatic_welcome','automatic_away','platform_question_card','coupon_card')) ORDER BY rowid DESC LIMIT 1",(task['account'],task['conversation'])).fetchone()
            versions={d['id']:d['source_sha256'] for d in self.shadow_documents()}
            fresh_product=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            changed=(not mode or mode[0]!='shadow' or fresh['status'] not in ('pending','human_review') or fresh['text']!=task['text'] or fresh['source']!=task['source'] or not tail or not inbound_version or tail[0]!=inbound_version[0])
            changed=changed or any(versions.get(ref['document_id'])!=ref['source_sha256'] for ref in result.get('evidence',[]))
            changed=changed or (json.loads(fresh_product[0]) if fresh_product else [])!=products
            if changed:raise ValueError('draft_changed')
            self.db.execute('INSERT INTO simulations(task,account,conversation,at,status,text,evidence,context) VALUES(?,?,?,?,?,?,?,?)',(task_id,task['account'],task['conversation'],datetime.now(timezone.utc).isoformat(),result['status'],result.get('text',''),json.dumps(result.get('evidence',[]),ensure_ascii=False),json.dumps({'products':products,'model':config.model},ensure_ascii=False)))
            self.audit('shadow_document_generation_only',task_id);self.db.commit()
        return {'status':result['status'],'reason':result.get('reason')}

    def approved_documents(self):
        from tongcheng_document_review import approved_documents
        reviews={r['id']:json.loads(r['payload']) for r in self.db.execute('SELECT * FROM document_reviews')}
        manual=approved_documents(self.shadow_documents(),reviews)
        identifiers={d['id'] for d in manual}
        return manual+[d for d in self.authorized_library() if d['id'] not in identifiers]

    def review_document(self,identifier,enabled,expires_at):
        from workbench_core.shadow_documents import public_row
        doc=next((d for d in self.shadow_documents() if d['id']==identifier and public_row(d)),None)
        if not doc:raise ValueError('document_not_found')
        now=datetime.now(timezone.utc)
        expiry=datetime.fromisoformat(expires_at)
        if not expiry.tzinfo or expiry<=now:raise ValueError('invalid_expiry')
        review={'enabled':bool(enabled),'valid_from':now.isoformat(),'expires_at':expiry.isoformat(),
                'source_sha256':doc['source_sha256'],'content_hash':doc['content_hash']}
        self.db.execute('INSERT INTO document_reviews VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(identifier,json.dumps(review)))
        self.db.execute("UPDATE tasks SET status='cancelled',reason='document_review_changed' WHERE source='documents_model' AND status='pending'")
        self.audit('document_approved' if enabled else 'document_disabled',identifier);self.db.commit()

    async def _generate_approved_documents(self,task_id,config,provider=None):
        from tongcheng_ownership import customer_question as merged_question
        from workbench_core.shadow_documents import document_draft
        from tongcheng_document_review import fingerprint
        async with self.lock:
            task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['status'] not in ('pending','human_review'):raise ValueError('task_not_editable')
            if self.is_human(task['account'],task['conversation']):raise ValueError('human_takeover')
            history=self.dialogue_history(task['account'],task['conversation'])
            question_history=history[:next((i+1 for i,m in enumerate(history) if m['id']==task['message']),0)]
            if not question_history or question_history[-1]['kind']!='text' or question_history[-1]['direction']!='inbound':raise ValueError('unsupported_message')
            # Following-card enrichment is queued by ingestion; any other later
            # inbound message has already cancelled the original question task.
            anchor=history[-1]['id']
            question=merged_question(question_history)
            if question is None:raise ValueError('unsupported_message')
            high_risk=any(w in question for w in ('价格','库存','余位','退款','退订','优惠','多少钱','赔偿','退费'))
            row=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            products=json.loads(row[0]) if row else []
            documents=self.approved_documents()
            from tongcheng_production import required_clarification
            prompt=required_clarification(question,history) if self.production_authorized() else None
            self.audit('required_condition_prompt_planned' if prompt else 'approved_document_model_requested',task_id);self.db.commit()
        result={'status':'shadow_clarification_ready','text':prompt,'evidence':[]} if prompt else await asyncio.to_thread(document_draft,question,question_history,documents,config,product=products,provider=provider,allow_clarification=True)
        from tongcheng_production import clarification_slot,clarification_fingerprint
        slot=clarification_slot(question,history,result.get('text','')) if result['status'] in ('shadow_clarification_ready','shadow_acknowledgement_ready') else None
        revision=self.production_revision()
        clarify=bool(slot and revision and self.production_authorized())
        if not clarify and (high_risk or result['status']!='shadow_draft_ready' or not result.get('evidence')):return {'status':'requires_human','reason':'dynamic_quote_requires_review' if high_risk else result.get('reason') or result['status']}
        if clarify:
            result=dict(result,evidence=[])
        async with self.lock:
            fresh=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            tail=self.db.execute("SELECT id FROM messages WHERE account=? AND conversation=? AND kind!='system' AND NOT EXISTS(SELECT 1 FROM message_origins o WHERE o.account=messages.account AND o.conversation=messages.conversation AND o.id=messages.id AND o.origin IN ('automatic_welcome','automatic_away','platform_question_card','coupon_card')) ORDER BY rowid DESC LIMIT 1",(task['account'],task['conversation'])).fetchone()
            row=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            current_products=json.loads(row[0]) if row else []
            digest=clarification_fingerprint(products,revision,slot,result['text']) if clarify else fingerprint(products,result['evidence'],documents)
            current_digest=clarification_fingerprint(current_products,self.production_revision(),slot,result['text']) if clarify and self.production_authorized() else fingerprint(current_products,result['evidence'],self.approved_documents())
            if not digest or digest!=current_digest or not tail or tail[0]!=anchor or not fresh or fresh['status'] not in ('pending','human_review') or fresh['text']!=task['text'] or fresh['source']!=task['source'] or self.is_human(task['account'],task['conversation']):raise ValueError('draft_changed')
            evidence=[dict(ref,reviewed=True) for ref in result['evidence']]
            self.db.execute("UPDATE tasks SET text=?,source=?,evidence=?,fingerprint=?,snapshot_anchor=?,status='pending',reason=?,knowledge_query=? WHERE id=?",(result['text'],('clarification_prompt' if prompt else 'clarification_model') if clarify else 'documents_model',json.dumps(evidence,ensure_ascii=False),digest,anchor,'missing_condition_only' if clarify else 'grounded_approved_product_knowledge',slot if clarify else None,task_id))
            self.audit('approved_document_draft_ready',task_id);self.db.commit()
        return {'status':'draft_ready'}

    async def _generate_task(self,task_id,config,provider=None):
        from workbench_core.knowledge import retrieve_faqs,select_with_model,validate_selection
        from tongcheng_ownership import customer_question as merged_question
        from workbench_core.contextual_knowledge import retrieve_contextual
        async with self.lock:
            task=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['status'] not in ('pending','human_review'):raise ValueError('task_not_editable')
            if self.is_human(task['account'],task['conversation']):raise ValueError('human_takeover')
            source=self.db.execute('SELECT * FROM messages WHERE account=? AND conversation=? AND id=?',(task['account'],task['conversation'],task['message'])).fetchone()
            if not source or source['kind']!='text' or source['direction']!='inbound':raise ValueError('unsupported_message')
            history=self.dialogue_history(task['account'],task['conversation'])
            if not history or history[-1]['id']!=task['message']:raise ValueError('conversation_changed')
            question=merged_question(history)
            if question is None:raise ValueError('unsupported_message')
            if any(w in question for w in ('价格','库存','余位','退款','优惠','多少钱','赔偿')):raise ValueError('requires_human')
            products=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            product=json.loads(products[0]) if products else None
            faqs=self.knowledge_for(task['account'],task['conversation'])
        retrieval,canonical,method=await asyncio.to_thread(retrieve_contextual,question,history,faqs,config,product=product)
        if method in ('contextual_model_error','invalid_model_input','ambiguous_knowledge','requires_human'):return {'status':method}
        selection=await asyncio.to_thread(select_with_model,retrieval,config,provider=provider)
        async with self.lock:
            fresh=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if fresh['status'] not in ('pending','human_review') or fresh['text']!=task['text'] or fresh['source']!=task['source'] or self.is_human(task['account'],task['conversation']):raise ValueError('draft_changed')
            current_product=self.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
            if (json.loads(current_product[0]) if current_product else None)!=product:raise ValueError('conversation_changed')
            result=validate_selection(retrieval,selection,self.knowledge_for(task['account'],task['conversation']),now=datetime.now(timezone.utc))
            if result.status=='draft_ready':
                self.db.execute("UPDATE tasks SET text=?,source='model',evidence=?,fingerprint=?,knowledge_query=?,status='pending',reason=? WHERE id=?",(result.draft,json.dumps([{'faq_id':result.faq_id,'version':result.version,'retrieval':method}]),result.fingerprint,canonical,'model_selected_reviewed_knowledge',task_id))
                self.db.commit()
            return {'status':result.status}

    def overview(self):
        policies=[dict(r) for r in self.db.execute('SELECT * FROM policies')]
        return {'state':self.state,'active':self.active,'auto':self.active and any(p['mode'] in ('test_auto','auto') for p in policies),'policies':policies,
                'simulations':[dict(r) for r in self.db.execute('SELECT simulations.*,tasks.status AS task_status FROM simulations LEFT JOIN tasks ON tasks.id=simulations.task ORDER BY simulations.id DESC LIMIT 100')],
                'last_error':self.last_error,
                'real_send_verified':bool(self.db.execute("SELECT 1 FROM tasks WHERE status='customer_confirmed' LIMIT 1").fetchone()),'handoffs':[dict(r) for r in self.db.execute("SELECT h.*,t.message,t.status AS notice_status FROM handoff_cases h JOIN tasks t ON h.task=t.id WHERE h.status IN ('open','in_progress') ORDER BY h.created_at DESC LIMIT 100")],'tasks':[dict(r) for r in self.db.execute('SELECT tasks.*, messages.text AS customer_text FROM tasks LEFT JOIN messages ON tasks.account=messages.account AND tasks.conversation=messages.conversation AND tasks.message=messages.id ORDER BY tasks.rowid DESC LIMIT 100')]}

