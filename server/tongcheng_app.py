"""Loopback-only Tongcheng console, isolated from demonstration API and Qianniu engine."""
import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse
from fastapi.exceptions import RequestValidationError
from contracts import OutboundMessageCommand
from tongcheng import TongchengService
from tongcheng_browser import BrowserTransport
from pydantic import BaseModel,Field
from workbench_core.knowledge import ModelConfig,FAQ,configure
from workbench_core.reply_queue import ReplyQueue
from tongcheng_model import provider as model_provider,status as live_model_status
from tongcheng_production import load_intent,valid_intent,library_documents,account_binding

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'artifacts' / 'tongcheng-private'

def knowledge():
    from qianniu_knowledge import FAQ
    path = PRIVATE / 'knowledge.json'
    return [FAQ.model_validate(item) for item in json.loads(path.read_text('utf-8'))] if path.exists() else []

@asynccontextmanager
async def lifespan(app):
    PRIVATE.mkdir(parents=True,exist_ok=True)
    config_path = PRIVATE / 'browser.json'
    config = json.loads(config_path.read_text('utf-8')) if config_path.exists() else {}
    if config.get('transport')=='independent_websocket':
        from tongcheng_direct import DirectTransport
        app.state.browser=DirectTransport(PRIVATE)
    else:app.state.browser = BrowserTransport(config,PRIVATE/'profile')
    app.state.service = TongchengService(PRIVATE/'state.sqlite',app.state.browser,knowledge)
    if config.get('transport')=='independent_websocket':
        for row in app.state.browser.db.execute("SELECT customer,id FROM observations WHERE message_type='11'"):
            scope=('local','ws:'+row['customer'],row['id'])
            app.state.service.db.execute("UPDATE messages SET kind='system' WHERE account=? AND conversation=? AND id=?",scope)
            app.state.service.db.execute("UPDATE tasks SET status='cancelled',reason='platform_notification' WHERE account=? AND conversation=? AND message=? AND status IN ('pending','human_review')",scope)
        app.state.service.db.commit()
    app.state.service.shadow_documents=lambda:json.loads((PRIVATE/'kdocs-knowledge.json').read_text('utf-8')).get('documents',[]) if (PRIVATE/'kdocs-knowledge.json').exists() else []
    app.state.service.knowledge_scopes=lambda:json.loads((PRIVATE/'knowledge-scopes.json').read_text('utf-8')) if (PRIVATE/'knowledge-scopes.json').exists() else {}
    from tongcheng_ownership import repair_roles
    repair_roles(app.state.service,app.state.browser)
    app.state.service.model_available=lambda: model_is_available()
    app.state.service.production_authorized=lambda:production_authorized()
    app.state.service.production_revision=lambda:load_intent(PRIVATE).get('revision')
    app.state.service.authorized_library=lambda:library_documents(app.state.service.shadow_documents(),load_intent(PRIVATE))
    from tongcheng_feishu import FeishuNotifications
    app.state.feishu=FeishuNotifications(PRIVATE,app.state.service,app.state.browser)
    app.state.notification_worker=asyncio.create_task(app.state.feishu.run())
    app.state.worker = None
    app.state.reply_queue=None
    login=PRIVATE/'operator-login.json'
    if not login.exists():
        shared=ROOT/'artifacts/workbench-private/operator-login.json'
        if PRIVATE==ROOT/'artifacts/tongcheng-private' and shared.exists():
            login.write_bytes(shared.read_bytes())
        else:
            login.write_text(json.dumps({'username':'operator','password':secrets.token_urlsafe(24)}),'utf-8')
    app.state.credentials=json.loads(login.read_text('utf-8'))
    app.state.sessions={}
    app.state.attempts=[]
    import faulthandler
    faulthandler.cancel_dump_traceback_later()
    yield
    app.state.notification_worker.cancel()
    await asyncio.gather(app.state.notification_worker,return_exceptions=True)
    app.state.feishu.close()
    if app.state.worker:
        app.state.worker.cancel()
        await asyncio.gather(app.state.worker,return_exceptions=True)
    if app.state.reply_queue:await app.state.reply_queue.close()
    await app.state.browser.close()
    app.state.service.db.close()

app = FastAPI(lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def invalid_request(request,exc):return JSONResponse({'detail':'invalid_request'},status_code=422)

class EditDraft(BaseModel):
    text:str=Field(min_length=1,max_length=8000)
    expected_message:str|None=None

class Login(BaseModel):
    username:str=Field(max_length=100)
    password:str=Field(max_length=200)

class Policy(BaseModel):
    mode:str
    conversations:list[str]=Field(default_factory=list,max_length=20)

@app.put('/policy')
async def policy(body:Policy):
    try:
        async with app.state.service.lock:
            app.state.service.policy('local',body.mode,body.conversations)
            if body.mode!='auto':
                intent=load_intent(PRIVATE)
                if intent:
                    intent['enabled']=False;save_production_intent(intent)
    except ValueError as e:raise HTTPException(409,str(e))
    return {'status':body.mode}

@app.get('/audit')
async def audit(limit:int=200,after_id:int=0):
    from datetime import datetime,timezone,timedelta
    async with app.state.service.lock:
        rows=[dict(r) for r in app.state.service.db.execute("SELECT a.*,t.conversation,t.message AS message_id,t.text AS current_reply,t.source AS current_source,t.status AS current_task_status,m.text AS customer_text,c.label AS customer_label FROM audit a LEFT JOIN tasks t ON a.entity=t.id LEFT JOIN messages m ON t.account=m.account AND t.conversation=m.conversation AND t.message=m.id LEFT JOIN conversation_meta c ON t.account=c.account AND t.conversation=c.conversation WHERE a.id>? ORDER BY a.id DESC LIMIT ?",(max(0,after_id),min(1000,max(1,limit))))]
        for row in rows:
            row['details']=json.loads(row['details'] or '{}')
            row['time_beijing']=datetime.fromisoformat(row['at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        return {'items':rows,'timezone':'Asia/Shanghai'}

@app.get('/knowledge')
async def knowledge_list():return {'items':[dict(f.model_dump(mode='json'),product_ids=app.state.service.knowledge_scopes().get(f.id,[])) for f in knowledge()]}

@app.get('/knowledge-inbox')
async def knowledge_inbox():
    path=PRIVATE/'knowledge-inbox.json'
    return {'items':json.loads(path.read_text('utf-8')) if path.exists() else []}

@app.get('/knowledge/documents')
async def document_sources():
    path=PRIVATE/'kdocs-knowledge.json'
    if not path.exists():return {'imported':False,'sheets':[],'documents':0}
    source=json.loads(path.read_text('utf-8'))
    return {'imported':True,'source_url':source['source_url'],'source_sha256':source['source_sha256'],'imported_at':source['imported_at'],
        'sheets':[s for s in source['sheets'] if s['state']=='visible'],'documents':sum(d['sheet_state']=='visible' for d in source['documents']),
        'shadow_only':not bool(app.state.service.approved_documents()),'embedded_images':source.get('embedded_images',0)}

@app.get('/shadow-tests/latest',response_class=HTMLResponse)
async def latest_shadow_test():
    index=PRIVATE/'shadow-replay-latest.json'
    if not index.exists():raise HTTPException(404,'shadow_replay_not_run')
    path=Path(json.loads(index.read_text('utf-8'))['report']).resolve()
    if not path.is_relative_to((PRIVATE/'shadow-tests').resolve()) or path.name!='report.html':raise HTTPException(409,'invalid_report_path')
    return HTMLResponse(path.read_text('utf-8'))

@app.get('/shadow-tests/knowledge',response_class=HTMLResponse)
async def latest_knowledge_test():
    index=PRIVATE/'knowledge-test-latest.json'
    if not index.exists():raise HTTPException(404,'knowledge_test_not_run')
    path=Path(json.loads(index.read_text('utf-8'))['report']).resolve()
    if not path.is_relative_to((PRIVATE/'knowledge-tests').resolve()) or path.name!='report.html':raise HTTPException(409,'invalid_report_path')
    return HTMLResponse(path.read_text('utf-8'))

@app.get('/shadow-tests/customers',response_class=HTMLResponse)
async def latest_customer_shadow():
    index=PRIVATE/'customer-shadow-latest.json'
    if not index.exists():raise HTTPException(404,'customer_shadow_not_run')
    path=Path(json.loads(index.read_text('utf-8'))['report']).resolve()
    if not path.is_relative_to((PRIVATE/'customer-shadow-tests').resolve()) or path.name!='report.html':raise HTTPException(409,'invalid_report_path')
    return HTMLResponse(path.read_text('utf-8'))

@app.get('/shadow/live',response_class=HTMLResponse)
async def live_shadow_page():
    return HTMLResponse((ROOT/'server/tongcheng_shadow_live.html').read_text('utf-8'))

@app.get('/health/resident')
async def resident_health():
    # The supervisor must not rebuild the complete customer/knowledge view on
    # every health probe, especially when the host is under memory pressure.
    service=app.state.service
    policy=service.db.execute('SELECT mode FROM policies WHERE account=?',('local',)).fetchone()
    wire=app.state.browser.wire_status
    return {'active':service.active,'mode':policy[0] if policy else 'draft',
            'transport':{'online':wire.get('online',False),'last_packet_at':wire.get('last_packet_at')}}

@app.get('/shadow/live/data')
async def live_shadow_data():
    service=app.state.service;transport=app.state.browser
    items=[]
    from workbench_core.shadow_documents import public_row
    knowledge_rows=[r for doc in service.shadow_documents() if (r:=public_row(doc))]
    recheck_path=PRIVATE/'live-shadow-rechecked.json'
    rechecked=json.loads(recheck_path.read_text('utf-8')) if recheck_path.exists() else {}
    model_path=PRIVATE/'model.json';model=json.loads(model_path.read_text('utf-8')) if model_path.exists() else {}
    # Only packets observed on the live socket. Browser history/replays are excluded.
    if transport.config.get('transport')=='independent_websocket':
        for row in transport.db.execute("SELECT i.*,o.received_at,c.label FROM inbox i JOIN observations o ON i.customer=o.customer AND i.id=o.id LEFT JOIN customers c ON c.id=i.customer WHERE i.direction='inbound' AND i.kind!='system' ORDER BY o.received_at DESC LIMIT 60"):
            item=dict(row);conversation='ws:'+row['customer']
            product=transport.db.execute('SELECT p.payload FROM product_cards p JOIN inbox i ON i.customer=p.customer AND i.id=p.id WHERE p.customer=? AND i.rowid<=(SELECT rowid FROM inbox WHERE customer=? AND id=?) ORDER BY i.rowid DESC LIMIT 1',(row['customer'],row['customer'],row['id'])).fetchone()
            item['products']=json.loads(product[0]) if product else []
            # Some clients send the question first and its card milliseconds later.
            # Preserve that chronology explicitly instead of asking which product.
            if row['kind']=='text':
                following=transport.db.execute("SELECT i.*,o.received_at,p.payload FROM inbox i JOIN observations o ON o.customer=i.customer AND o.id=i.id LEFT JOIN product_cards p ON p.customer=i.customer AND p.id=i.id WHERE i.customer=? AND i.kind!='system' AND i.rowid>(SELECT rowid FROM inbox WHERE customer=? AND id=?) ORDER BY i.rowid LIMIT 1",(row['customer'],row['customer'],row['id'])).fetchone()
                if following:
                    from tongcheng_shadow_context import following_question
                    card=dict(following);card['products']=json.loads(card['payload']) if card['payload'] else []
                    if following_question(item,card):
                        item['products']=card['products']
                        item['product_context_source']='immediately_following_card'
                        item['product_context_message_id']=card['id']
            identifiers={p['external_product_id'] for p in item['products']}
            item['product_knowledge']=[{'id':r['id'],'sheet':r['sheet'],'row':r['row']} for r in knowledge_rows if identifiers.intersection(r['product_ids'])]
            sim=service.db.execute('SELECT s.* FROM simulations s JOIN tasks t ON t.id=s.task WHERE t.account=? AND t.conversation=? AND t.message=? ORDER BY s.id DESC LIMIT 1',('local',conversation,row['id'])).fetchone()
            item['simulation']=dict(sim) if sim else None
            if sim:
                context=json.loads(sim['context'] or '{}')
                item['simulation']['product_context_verified']=bool('products' in context and context['products']==item['products'])
            corrected=next((r for r in rechecked.get('results',[]) if r['customer']==row['customer'] and r['message_id']==row['id'] and r['question']==row['text'] and r['products']==item['products']),None)
            if corrected and knowledge_rows and rechecked.get('model')==model.get('model') and rechecked.get('source_sha256')==knowledge_rows[0]['source_sha256'] and (not sim or corrected['at']>sim['at']):
                item['simulation']={'at':corrected['at'],'status':corrected['status'],'text':corrected['text'],'evidence':json.dumps(corrected['evidence'],ensure_ascii=False),'recomputed':True,'product_context_verified':True}
            task=service.db.execute('SELECT id,status,reason,text,source,evidence FROM tasks WHERE account=? AND conversation=? AND message=?',('local',conversation,row['id'])).fetchone()
            item['task']=dict(task) if task else None
            item['human_owned']=service.is_human('local',conversation)
            items.append(item)
        types=[dict(r) for r in transport.db.execute('SELECT message_type,content_type,count(*) AS count FROM observations GROUP BY message_type,content_type')]
    else:types=[]
    model_path=PRIVATE/'model.json';model=json.loads(model_path.read_text('utf-8')) if model_path.exists() else {}
    policy=service.db.execute('SELECT mode FROM policies WHERE account=?',('local',)).fetchone()
    return {'now':time.time(),'active':service.active,'transport':transport.wire_status,'mode':policy[0] if policy else 'draft',
        'model':{'enabled':bool(model.get('enabled') and model.get('api_key')),'name':model.get('model'),
            'live_requests':service.db.execute("SELECT count(*) FROM audit a JOIN tasks t ON t.id=a.entity WHERE a.action IN ('shadow_model_requested','approved_document_model_requested') AND t.conversation LIKE 'ws:%'").fetchone()[0]},'items':items,'event_types':types,'historical_replay':False}

@app.put('/knowledge/{faq_id}')
async def knowledge_save(faq_id:str,body:FAQ,product_id:str='',source_ref:str=''):
    if faq_id!=body.id:raise HTTPException(409,'knowledge_identity_mismatch')
    async with app.state.service.lock:
        items=list(knowledge());old=next((f for f in items if f.id==faq_id),None)
        if old and body.version!=old.version+1:raise HTTPException(409,'knowledge_version_changed')
        source=None
        if source_ref:
            inbox=PRIVATE/'knowledge-inbox.json'
            source=next((s for s in json.loads(inbox.read_text('utf-8')) if s['id']==source_ref),None) if inbox.exists() else None
            if source is None:raise HTTPException(409,'knowledge_source_changed')
            if (source.get('original_item_id') or source.get('source_type')=='chat') and not product_id:raise HTTPException(409,'source_requires_product_mapping')
        if len(product_id)>100:raise HTTPException(422,'invalid_product_id')
        scopes=app.state.service.knowledge_scopes()
        scopes[faq_id]=[product_id] if product_id else []
        scope_path=PRIVATE/'knowledge-scopes.json';scope_temp=scope_path.with_suffix('.tmp')
        scope_temp.write_text(json.dumps(scopes),'utf-8');scope_temp.replace(scope_path)
        items=[f for f in items if f.id!=faq_id]+[body]
        path=PRIVATE/'knowledge.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps([f.model_dump(mode='json') for f in items],ensure_ascii=False),'utf-8');temp.replace(path)
        if source:
            provenance=PRIVATE/'knowledge-provenance.json'
            records=json.loads(provenance.read_text('utf-8')) if provenance.exists() else []
            records.append({'faq_id':faq_id,'source_ref':source_ref,'source_hash':source['content_hash'],'original_item_id':source['original_item_id'],'tongcheng_product_id':product_id,'reviewed':body.reviewed,'source_platform':'xianyu'})
            provenance_temp=provenance.with_suffix('.tmp');provenance_temp.write_text(json.dumps(records,ensure_ascii=False),'utf-8');provenance_temp.replace(provenance)
        app.state.service.db.execute("UPDATE tasks SET status='cancelled',reason='knowledge_changed' WHERE status='pending' AND source!='manual'")
        app.state.service.audit('knowledge_updated',faq_id);app.state.service.db.commit()
    return {'status':'saved'}

def model_config():
    path=PRIVATE/'model.json'
    return ModelConfig(**json.loads(path.read_text('utf-8'))) if path.exists() else ModelConfig()

def model_is_available():
    state=live_model_status(PRIVATE,model_config())
    return state.get('state')=='available' and time.time()-state.get('checked_at',0)<600

@app.post('/model/check')
async def model_check():
    config=model_config()
    payload={'model':config.model,'stream':False,'max_tokens':32,'messages':[{'role':'user','content':'只回复：连接正常'}]}
    if 'ark.cn-beijing.volces.com' in config.endpoint:
        payload['thinking']={'type':'disabled'};payload['max_completion_tokens']=payload.pop('max_tokens')
    try:await asyncio.to_thread(model_provider(PRIVATE,config,probe=True),payload)
    except Exception:pass
    return live_model_status(PRIVATE,config)

class DocumentReview(BaseModel):
    enabled:bool
    expires_at:str

@app.get('/knowledge/documents/rows')
async def document_rows(query:str=''):
    from workbench_core.shadow_documents import public_row
    usable={d['id'] for d in app.state.service.approved_documents()}
    rows=[dict(r,approved=r['id'] in usable) for d in app.state.service.shadow_documents() if (r:=public_row(d)) and (not query or query.lower() in json.dumps(r,ensure_ascii=False).lower())]
    return {'items':rows[:100],'total':len(rows)}

@app.put('/knowledge/documents/{identifier}/review')
async def document_review(identifier:str,body:DocumentReview):
    try:
        async with app.state.service.lock:app.state.service.review_document(identifier,body.enabled,body.expires_at)
    except ValueError as exc:raise HTTPException(409,str(exc))
    return {'status':'approved' if body.enabled else 'disabled'}

@app.get('/model')
async def model_status():
    path=PRIVATE/'model.json'
    return dict(configure(model_config()),live=live_model_status(PRIVATE,model_config()))

@app.post('/review/{task_id}')
async def review(task_id:str):
    async with app.state.service.lock:
        db=app.state.service.db
        changed=db.execute("UPDATE tasks SET status='reviewed',reason='operator_reviewed_no_resend' WHERE id=? AND status='uncertain'",(task_id,)).rowcount
        if not changed:raise HTTPException(409,'task_not_reviewable')
        app.state.service.audit('uncertain_task_reviewed',task_id);db.commit()
    return {'status':'reviewed'}

@app.post('/confirm-delivery/{task_id}')
async def confirm_delivery(task_id:str):
    async with app.state.service.lock:
        db=app.state.service.db
        changed=db.execute("UPDATE tasks SET status='customer_confirmed',reason='operator_confirmed_customer_received' WHERE id=? AND status='server_accepted'",(task_id,)).rowcount
        if not changed:raise HTTPException(409,'task_not_confirmable')
        app.state.service.audit('operator_confirmed_customer_received',task_id);db.commit()
    return {'status':'customer_confirmed'}

@app.post('/login-browser')
async def login_browser():
    await pause()
    async with app.state.service.lock:
        if app.state.browser.config.get('transport')=='independent_websocket':
            await app.state.browser.close()
            path=PRIVATE/'browser.json';config=json.loads(path.read_text('utf-8'));config.pop('transport',None)
            path.write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
            browser=BrowserTransport(config,PRIVATE/'profile')
            app.state.browser=browser;app.state.service.transport=browser
    return await open_browser()

@app.post('/auth/login')
async def login(body:Login):
    app.state.attempts=[t for t in app.state.attempts if t>time.monotonic()-60]
    if len(app.state.attempts)>=10:raise HTTPException(429,'try_later')
    app.state.attempts.append(time.monotonic())
    c=app.state.credentials
    if not (secrets.compare_digest(body.username,c['username']) and secrets.compare_digest(body.password,c['password'])):raise HTTPException(401,'invalid_login')
    sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
    app.state.sessions[sid]=(csrf,time.monotonic()+28800)
    response=JSONResponse({'csrf':csrf})
    response.set_cookie('tc_session',sid,httponly=True,samesite='strict',max_age=28800)
    return response

@app.get('/auth/me')
async def me(request:Request):
    return {'csrf':app.state.sessions[request.cookies.get('tc_session')][0]}

@app.put('/draft/{task_id}')
async def edit_draft(task_id:str,body:EditDraft):
    try:
        async with app.state.service.lock:app.state.service.edit_task(task_id,body.text)
    except ValueError as e:raise HTTPException(409,str(e))
    return {'status':'saved'}

@app.post('/generate/{task_id}')
async def generate(task_id:str):
    path=PRIVATE/'model.json'
    config=ModelConfig(**json.loads(path.read_text('utf-8'))) if path.exists() else ModelConfig()
    try:return await app.state.service.generate_task(task_id,config,provider=model_provider(PRIVATE,config))
    except ValueError as e:raise HTTPException(409,str(e))

@app.middleware('http')
async def local_only(request:Request, call_next):
    # Explicit company-LAN allowlist, plus same-origin and authenticated writes.
    from tongcheng_network import allowed,config as network_config
    if not allowed(request.client.host,request.url.hostname,network_config(PRIVATE)):
        return HTMLResponse('Network access denied',status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return HTMLResponse('Origin rejected',status_code=403)
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return HTMLResponse('Cross-site rejected',status_code=403)
    if request.url.path not in ('/','/auth/login'):
        session=app.state.sessions.get(request.cookies.get('tc_session',''))
        if not session or session[1]<time.monotonic():
            if request.method=='GET' and request.url.path=='/shadow/live':return RedirectResponse('/',status_code=303)
            return JSONResponse({'detail':'login_required'},status_code=401)
        if request.method!='GET' and not secrets.compare_digest(request.headers.get('x-csrf-token',''),session[0]):return JSONResponse({'detail':'csrf_required'},status_code=403)
    return await call_next(request)

@app.get('/',response_class=HTMLResponse)
def console():
    return (ROOT/'server'/'tongcheng_console.html').read_text('utf-8')

@app.get('/state')
def state():
    result=app.state.service.overview()
    result['transport']=app.state.browser.wire_status
    result['notifications']=app.state.feishu.status()
    return result

@app.get('/conversations')
async def conversations():
    return {'items':[dict(r) for r in app.state.service.db.execute('''SELECT m.account,m.conversation,count(*) AS message_count,c.label,c.reference_source,
        coalesce(s.human,0) AS human,
        EXISTS(SELECT 1 FROM handoff_cases h WHERE h.account=m.account AND h.conversation=m.conversation AND h.status IN ('open','in_progress')) AS needs_human,
        (SELECT text FROM messages t WHERE t.account=m.account AND t.conversation=m.conversation ORDER BY t.rowid DESC LIMIT 1) AS preview,
        (SELECT kind FROM messages t WHERE t.account=m.account AND t.conversation=m.conversation ORDER BY t.rowid DESC LIMIT 1) AS preview_kind
        FROM messages m LEFT JOIN conversation_meta c ON c.account=m.account AND c.conversation=m.conversation
        LEFT JOIN sessions s ON s.account=m.account AND s.conversation=m.conversation
        GROUP BY m.account,m.conversation ORDER BY max(m.rowid) DESC''')]}

@app.get('/conversations/{conversation}')
async def conversation_detail(conversation:str):
    from tongcheng_message_times import conversation_messages
    product=app.state.service.db.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',('local',conversation)).fetchone()
    return {'messages':conversation_messages(app.state.service,app.state.browser,conversation), 'products':json.loads(product[0]) if product else []}

@app.post('/conversations/{conversation}/draft')
async def conversation_draft(conversation:str,body:EditDraft):
    try:
        async with app.state.service.lock:task=app.state.service.manual_reply('local',conversation,body.text,body.expected_message)
    except ValueError as e:raise HTTPException(409,str(e))
    return {'status':'saved','task_id':task}

@app.post('/conversations/{conversation}/generate')
async def conversation_generate(conversation:str):
    service=app.state.service
    async with service.lock:
        tail=service.db.execute('SELECT * FROM messages WHERE account=? AND conversation=? ORDER BY rowid DESC LIMIT 1',('local',conversation)).fetchone()
        if not tail or tail['direction']!='inbound' or tail['kind']!='text':raise HTTPException(409,'unsupported_message')
        task=service.db.execute('SELECT id,status FROM tasks WHERE account=? AND conversation=? AND message=?',('local',conversation,tail['id'])).fetchone()
        if not task:
            import uuid
            task_id=uuid.uuid4().hex
            service.db.execute("INSERT INTO tasks(id,account,conversation,message,text,status,reason) VALUES(?,?,?,?,'','human_review','operator_requested_generation')",(task_id,'local',conversation,tail['id']))
            service.audit('operator_requested_generation',task_id);service.db.commit()
        else:task_id=task['id']
    path=PRIVATE/'model.json'
    config=ModelConfig(**json.loads(path.read_text('utf-8'))) if path.exists() else ModelConfig()
    try:return await service.generate_task(task_id,config,provider=model_provider(PRIVATE,config))
    except ValueError as e:raise HTTPException(409,str(e))

@app.post('/conversations/{conversation}/handoff/{mode}')
async def conversation_handoff(conversation:str,mode:str):
    if mode not in ('human','ai'):raise HTTPException(422,'invalid_mode')
    async with app.state.service.lock:
        if not app.state.service.db.execute('SELECT 1 FROM conversation_meta WHERE account=? AND conversation=?',('local',conversation)).fetchone():raise HTTPException(404,'conversation_not_found')
        app.state.service.handoff('local',conversation,mode=='human')
    return {'status':mode}

@app.post('/open')
async def open_browser():
    async with app.state.service.lock:
        if app.state.browser.config.get('transport')=='independent_websocket':
            await app.state.browser.open()
            return {'status':'connected','transport':'independent_websocket'}
        context=app.state.browser.context
        if context and (not context.pages or (context.browser and not context.browser.is_connected())):
            await app.state.browser.close()
            app.state.browser.context=None
        if not app.state.browser.context:
            await app.state.browser.open()
    return {'status':'browser_opened','instruction':'请在专用浏览器登录并打开客服工作台'}

async def process_reply(key,event):
    service=app.state.service
    task=service.db.execute('SELECT * FROM tasks WHERE account=? AND conversation=? AND message=?',('local',event.external_conversation_id,event.external_message_id)).fetchone()
    if not task or not service.active or task['source']=='manual' or task['status'] not in ('pending','human_review'):return
    if event.content.get('type')!='text':
        service.db.execute("UPDATE tasks SET status='human_review',reason='unsupported_message' WHERE id=? AND status IN ('pending','human_review')",(task['id'],))
        service.audit('customer_attachment_requires_review',task['id'],reason='unsupported_message');service.db.commit();return
    current_policy=service.db.execute("SELECT mode FROM policies WHERE account='local'").fetchone()
    if service.is_human('local',event.external_conversation_id) and (not current_policy or current_policy[0]!='shadow'):
        service.db.execute("UPDATE tasks SET status='human_review',reason='human_takeover' WHERE id=? AND status IN ('pending','human_review')",(task['id'],))
        service.audit('reply_generation_skipped',task['id'],reason='human_takeover');service.db.commit();return
    path=PRIVATE/'model.json'
    if not path.exists():
        service.audit('human_review_required',task['id'],reason='model_unconfigured')
        service.db.execute("UPDATE tasks SET status='human_review',reason='model_unconfigured' WHERE id=? AND status IN ('pending','human_review')",(task['id'],));service.db.commit();return
    try:
        config=model_config();generation_started=time.monotonic()
        if not model_is_available() and live_model_status(PRIVATE,config).get('retry_after',0)<=time.time():
            await model_check()
        service.audit('reply_generation_started',task['id'],model=config.model);service.db.commit()
        result=await service.generate_task(task['id'],config,provider=model_provider(PRIVATE,config))
        generated=service.db.execute('SELECT text,source FROM tasks WHERE id=?',(task['id'],)).fetchone()
        service.audit('reply_generation_completed',task['id'],model=config.model,status=result['status'],reason=result.get('reason',''),duration_ms=int((time.monotonic()-generation_started)*1000),source=generated['source'] if generated else '',reply_length=len(generated['text']) if generated else 0);service.db.commit()
        if result['status']!='draft_ready':
            service.audit('human_review_required',task['id'],reason=result['status'])
            service.db.execute("UPDATE tasks SET status='human_review',reason=? WHERE id=? AND status IN ('pending','human_review') AND source!='manual'",(result['status'],task['id']));service.db.commit()
        if result['status'] in ('requires_human','source_insufficient','no_source_match','shadow_model_or_citation_error','invalid_model_input'):
            async with service.lock:
                if service.prepare_handoff_notice(task['id'],result.get('reason') or result['status']):result={'status':'draft_ready'}
        policy=service.db.execute('SELECT * FROM policies WHERE account=?',('local',)).fetchone()
        if result['status']=='draft_ready' and policy and (policy['mode']=='auto' or (policy['mode']=='test_auto' and event.external_conversation_id in json.loads(policy['conversations']))):
            fresh=service.db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
            receipt=await service.send_message(OutboundMessageCommand('local',fresh['conversation'],{'type':'text','text':fresh['text']},fresh['id']))
            service.audit('automatic_send_result',fresh['id'],status='accepted' if receipt.accepted else 'not_accepted',reason=receipt.error or '');service.db.commit()
            if not receipt.accepted:
                service.db.execute("UPDATE tasks SET status='human_review',reason=? WHERE id=? AND status='pending'",(receipt.error or 'send_not_completed',fresh['id']));service.db.commit()
    except Exception as exc:
        controlled={'human_takeover','task_not_editable','conversation_changed','draft_changed','requires_human','unsupported_message','shadow_mode_required'}
        reason=str(exc) if isinstance(exc,ValueError) and str(exc) in controlled else 'model_error'
        service.audit('reply_generation_skipped' if reason in controlled else 'reply_generation_failed',task['id'],reason=reason,exception_type=type(exc).__name__)
        service.db.execute("UPDATE tasks SET status='human_review',reason=? WHERE id=? AND status IN ('pending','human_review') AND source!='manual'",(reason,task['id']))
        service.audit('draft_generation_requires_review',task['id'],reason=reason);service.db.commit()

async def consume():
    async for event in app.state.service.listen('local'):
        accepted=app.state.reply_queue.submit(('tongcheng','local',event.external_conversation_id),event)
        if not accepted:
            app.state.service.db.execute("UPDATE tasks SET status='human_review',reason='queue_full_requires_human' WHERE account='local' AND conversation=? AND message=? AND status IN ('pending','human_review')",(event.external_conversation_id,event.external_message_id))
            app.state.service.audit('queue_full_requires_human',event.external_conversation_id);app.state.service.db.commit()

def production_authorized():
    intent=load_intent(PRIVATE)
    return valid_intent(intent) and bool(intent.get('account_binding')) and intent['account_binding']==account_binding(app.state.browser)


def save_production_intent(value):
    target=PRIVATE/'production-intent.json';temp=target.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8');temp.replace(target)


def monitor_desired(enabled):
    # A deliberate pause survives service/supervisor restarts.
    target=PRIVATE/'resident-control.json'
    temp=target.with_suffix('.tmp')
    temp.write_text(json.dumps({'monitor_enabled':bool(enabled)}),'utf-8')
    temp.replace(target)

@app.post('/start')
async def start():
    if not app.state.browser.config.get('calibrated'):
        raise HTTPException(409,'页面尚未校准：请完成只读核验')
    if app.state.worker and not app.state.worker.done():
        monitor_desired(True)
        return {'status':'already_running'}
    health = await app.state.service.connect('local')
    if health.status != 'connected':
        raise HTTPException(409,health.detail or health.status)
    app.state.reply_queue=ReplyQueue(process_reply)
    app.state.worker = asyncio.create_task(consume())
    monitor_desired(True)
    return {'status':'connected'}

@app.post('/pause')
async def pause():
    monitor_desired(False)
    await app.state.service.disconnect('local')
    if app.state.worker:
        app.state.worker.cancel()
        await asyncio.gather(app.state.worker,return_exceptions=True)
    if app.state.reply_queue:await app.state.reply_queue.close()
    return {'status':'paused'}



class FeishuSettings(BaseModel):
    enabled:bool
    webhook:str|None=Field(default=None,max_length=2048)
    secret:str|None=Field(default=None,max_length=500)

@app.get('/notifications/feishu')
async def feishu_status():return app.state.feishu.status()

@app.put('/notifications/feishu')
async def feishu_settings(body:FeishuSettings):
    try:return app.state.feishu.save(body.enabled,body.webhook,body.secret)
    except ValueError:raise HTTPException(409,'invalid_feishu_configuration')

@app.post('/notifications/feishu/test')
async def feishu_test():
    try:return await app.state.feishu.test()
    except ValueError:raise HTTPException(409,'feishu_webhook_required')

class ProductionLaunch(BaseModel):
    authorize_live_sending:bool
    knowledge_source_sha256:str=Field(min_length=64,max_length=64)
    user_instruction:str=Field(min_length=1,max_length=500)

@app.get('/production')
async def production_status():
    intent=load_intent(PRIVATE)
    return {'authorized':production_authorized(),'revision':intent.get('revision'),'expires_at':intent.get('expires_at'),
            'authorized_product_rows':len(app.state.service.authorized_library()),
            'mode':(app.state.service.overview()['policies'] or [{'mode':'draft'}])[0]['mode'],
            'customer_delivery_verified':app.state.service.overview()['real_send_verified']}

@app.post('/production/enable')
async def production_enable(body:ProductionLaunch):
    from datetime import datetime,timezone,timedelta
    from uuid import uuid4
    from workbench_core.shadow_documents import public_row
    if not body.authorize_live_sending:raise HTTPException(409,'explicit_live_authorization_required')
    service=app.state.service
    if not model_is_available():
        if (await model_check()).get('state')!='available':raise HTTPException(409,'model_call_not_verified')
    # Finish connection verification before writing durable sending authority.
    await open_browser();await start()
    if not app.state.browser.wire_status.get('online') or not account_binding(app.state.browser):raise HTTPException(409,'connection_not_ready')
    async with service.lock:
        documents=service.shadow_documents()
        rows={d['id']:d['content_hash'] for d in documents if d['source_sha256']==body.knowledge_source_sha256 and (r:=public_row(d)) and r['product_ids']}
        if not rows:raise HTTPException(409,'knowledge_version_changed')
        intent={'enabled':True,'authority':'explicit_user_live_launch','revision':uuid4().hex,'authorized_at':datetime.now(timezone.utc).isoformat(),
                'expires_at':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),'source_sha256':body.knowledge_source_sha256,
                'rows':rows,'account_binding':account_binding(app.state.browser),'user_instruction':body.user_instruction,'customer_delivery_verified_at_launch':bool(service.overview()['real_send_verified'])}
        save_production_intent(intent)
        try:service.policy('local','auto')
        except Exception:
            intent['enabled']=False;save_production_intent(intent);raise
        # The explicit launch hands existing conversations back once. Subsequent
        # human replies/takeover immediately regain ownership as usual.
        service.db.execute("UPDATE sessions SET human=0 WHERE account='local' AND NOT EXISTS (SELECT 1 FROM audit a WHERE a.entity=sessions.conversation AND a.id=(SELECT max(b.id) FROM audit b WHERE b.entity=sessions.conversation AND b.action IN ('human_takeover','return_to_ai')) AND a.action='human_takeover')")
        service.audit('production_user_authorized_launch',intent['revision']);service.db.commit()
    return await production_status()

@app.post('/production/resume')
async def production_resume():
    intent=load_intent(PRIVATE)
    control=json.loads((PRIVATE/'resident-control.json').read_text('utf-8')) if (PRIVATE/'resident-control.json').exists() else {}
    if not valid_intent(intent) or not control.get('monitor_enabled'):return {'status':'not_authorized_or_paused'}
    mode=app.state.service.db.execute("SELECT mode FROM policies WHERE account='local'").fetchone()
    if mode and mode[0]=='auto' and app.state.service.active:return {'status':'already_running'}
    runtime=live_model_status(PRIVATE,model_config())
    if runtime.get('retry_after',0)>time.time():return {'status':'model_unavailable'}
    if not model_is_available() and (await model_check()).get('state')!='available':return {'status':'model_unavailable'}
    await open_browser();await start()
    async with app.state.service.lock:
        fresh=load_intent(PRIVATE);control=json.loads((PRIVATE/'resident-control.json').read_text('utf-8'))
        if not valid_intent(fresh) or fresh.get('revision')!=intent['revision'] or not control.get('monitor_enabled'):return {'status':'not_authorized_or_paused'}
        if not app.state.browser.wire_status.get('online') or not production_authorized():return {'status':'connection_not_ready'}
        app.state.service.policy('local','auto')
        app.state.service.audit('production_authorized_resume',intent['revision']);app.state.service.db.commit()
    return {'status':'auto_resumed'}

@app.post('/handoff/{task_id}/{mode}')
async def handoff(task_id:str,mode:str):
    task = app.state.service.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task or mode not in ('human','ai'):
        raise HTTPException(404)
    async with app.state.service.lock:
        app.state.service.handoff(task['account'],task['conversation'],mode == 'human')
    return {'status':mode}

@app.post('/probe')
async def probe():
    try:
        path=PRIVATE/'browser.json'
        if path.exists():app.state.browser.config=json.loads(path.read_text('utf-8'))
        async with app.state.service.lock:
            snap = await app.state.browser.snapshot()
        return {'logged_in':snap.get('logged_in',False),'identity_verified':snap.get('identity_verified',False),
            'account_verified':app.state.browser.account_verified,'chat_tabs':sum(1 for p in app.state.browser.context.pages if 'kefu.17u.cn/' in p.url) if app.state.browser.context else 0,
            'message_count':len(snap.get('messages',[])), 'input_empty':not snap.get('input_text'),
            'real_send_verified':False}
    except Exception:
        raise HTTPException(409,'browser_unavailable_or_not_calibrated')

@app.get('/browser-diagnostics')
async def browser_diagnostics():
    from urllib.parse import urlsplit
    browser=app.state.browser
    async with app.state.service.lock:
        pages=[]
        for page in browser.context.pages if browser.context else []:
            parsed=urlsplit(page.url)
            pages.append({'host':parsed.hostname,'path':parsed.path,'closed':page.is_closed(),
                'frames':[{'host':urlsplit(f.url).hostname,'path':urlsplit(f.url).path} for f in page.frames]})
        try:
            snap=await browser.snapshot();failure=snap.get('reason')
        except ValueError as exc:failure=str(exc) if str(exc).replace('_','').isalnum() else 'invalid_page_state'
        except Exception as exc:failure=type(exc).__name__
        return {'pages':pages,'failure':failure,'account_verified':browser.account_verified}

@app.post('/websocket/probe')
async def websocket_probe():
    from tongcheng_sdk import ATTACH
    async with app.state.service.lock:
        browser=app.state.browser
        if browser.config.get('transport')=='independent_websocket':return browser.wire_status
        snap=await browser.snapshot()
        if not snap.get('logged_in') or not browser.account_verified:raise HTTPException(409,'verified_login_required')
        result=await browser.page.evaluate(ATTACH)
        # Customer identifiers stay in connector state, not public diagnostics.
        result.pop('current_customer_id',None)
        return result

@app.post('/websocket/independent')
async def websocket_independent():
    # Explicit local operator action: transfer ownership, never run two reply owners.
    if app.state.browser.config.get('transport')=='independent_websocket':return app.state.browser.wire_status
    await pause()
    async with app.state.service.lock:
        browser=app.state.browser
        snap=await browser.snapshot()
        if not snap.get('identity_verified') or not browser.account_verified:raise HTTPException(409,'verified_login_required')
        from tongcheng_sdk import ATTACH
        probe=await browser.page.evaluate(ATTACH)
        if not probe.get('online'):raise HTTPException(409,'sdk_not_online')
        session=await browser.page.evaluate('''() => {
            const sdk=window.__tcWorkbenchWire.sdk,l=sdk.loginInfo||{},u=sdk.localUserInfo||{},out={};
            for(const k of ['userId','spaceId','appId','deviceId','accessToken','refreshToken','accessTokenExpireInTimestamp','refreshTokenExpireInTimestamp'])if(l[k]!==undefined)out[k]=l[k];
            out.source_uid=String(sdk.srcUid||'');out.tenant_code=u.tenant_code;return out;
        }''')
        required=('userId','spaceId','appId','deviceId','accessToken','source_uid','tenant_code')
        if not all(session.get(k) for k in required):raise HTTPException(409,'sdk_session_incomplete')
        path=PRIVATE/'wire-session.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(session),'utf-8');temp.replace(path)
        await browser.close()
        from tongcheng_direct import DirectTransport
        direct=DirectTransport(PRIVATE);app.state.browser=direct;app.state.service.transport=direct
        config_path=PRIVATE/'browser.json';config=json.loads(config_path.read_text('utf-8'));config['transport']='independent_websocket'
        config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
        await direct.open()
        app.state.service.audit('independent_websocket_enabled','local');app.state.service.db.commit()
    return await start()

@app.get('/readiness')
async def readiness(conversation:str=''):
    """Current prerequisites only; never infer successful delivery from a DOM echo."""
    from datetime import datetime,timezone
    service=app.state.service
    async with service.lock:
        try:
            snap=await app.state.browser.snapshot()
            browser_error=None
        except Exception:
            snap={};browser_error='browser_unavailable_or_customer_not_verified'
        now=datetime.now(timezone.utc)
        faqs=service.knowledge_for('local',conversation) if conversation else knowledge()
        usable=[f for f in faqs if f.reviewed and f.enabled and f.valid_from<=now<f.expires_at]
        model=await model_status()
        connected=bool(snap.get('logged_in') and snap.get('identity_verified'))
        checks=[
            {'id':'connection','ok':connected,'label':'同程咨询页与客户身份已核验'},
            {'id':'listener','ok':bool(service.active and app.state.worker and not app.state.worker.done()),'label':'新消息监听运行中'},
            {'id':'model','ok':bool(model.get('configured') and model.get('enabled')),'label':'模型已配置（不代表本次调用成功）'},
            {'id':'knowledge','ok':bool(usable),'label':'当前范围有有效审核知识','count':len(usable)},
        ]
        target=bool(conversation and snap.get('conversation_id')==conversation)
        return {'checks':checks,'manual_test_ready':all(c['ok'] for c in checks[:2]),
            'draft_prerequisites_ready':all(c['ok'] for c in checks),
            'selected_target_verified':bool(target and snap.get('send_identity_verified',False)),
            'input_empty':not bool(snap.get('input_text')),'browser_error':browser_error,
            'real_send_acceptance':'customer_confirmed' if service.db.execute("SELECT 1 FROM tasks WHERE account='local' AND status='customer_confirmed' LIMIT 1").fetchone() else 'not_accepted','delivery_receipt_supported':False,'auto_reply_ready':bool(connected and service.active and app.state.worker and not app.state.worker.done() and model_is_available() and (usable or service.approved_documents()) and (service.production_authorized() or service.db.execute("SELECT 1 FROM tasks WHERE account='local' AND status='customer_confirmed' LIMIT 1").fetchone())),'approved_product_rows':len(service.approved_documents()),'model_live':live_model_status(PRIVATE,model_config()),'production_authorized':production_authorized()}

@app.post('/send/{task_id}')
async def send(task_id:str):
    task = app.state.service.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:
        raise HTTPException(404)
    return await app.state.service.send_message(OutboundMessageCommand(task['account'],task['conversation'],
        {'type':'text','text':task['text']},task_id))
