"""Replay real historical questions in an isolated DB with real configured model.

Never imports the network transport; never writes production messages/tasks.
"""
import asyncio
import hashlib
import html
import json
import sqlite3
import sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'server'))
from tongcheng import TongchengService
from contracts import OutboundMessageCommand
from workbench_core.knowledge import FAQ,ModelConfig

PRIVATE=ROOT/'artifacts/tongcheng-private'

class NoSendTransport:
    def __init__(self,snapshot):self.s=snapshot;self.calls=0
    async def snapshot(self):return self.s
    async def send_checked(self,*args):
        self.calls+=1
        raise AssertionError('isolated_replay_must_never_send')

async def main():
    source=sqlite3.connect((PRIVATE/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    source.row_factory=sqlite3.Row
    faqs=[FAQ.model_validate(f) for f in json.loads((PRIVATE/'knowledge.json').read_text('utf-8'))]
    config=ModelConfig(**json.loads((PRIVATE/'model.json').read_text('utf-8')))
    scopes=json.loads((PRIVATE/'knowledge-scopes.json').read_text('utf-8')) if (PRIVATE/'knowledge-scopes.json').exists() else {}
    candidates=list(source.execute("SELECT rowid,* FROM messages WHERE direction='inbound' AND kind='text' ORDER BY rowid DESC LIMIT 1000"))
    samples=[]
    for faq in faqs:
        match=next((r for r in candidates if r['text'] in (faq.question,*faq.aliases)),None)
        if match:samples.append(match);break
    for row in candidates:
        if row['text'].strip() and row['text'] not in [r['text'] for r in samples]:samples.append(row)
        if len(samples)>=3:break
    out=PRIVATE/'shadow-tests'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True,exist_ok=True)
    results=[]
    for index,row in enumerate(samples):
        conversation='replay:'+hashlib.sha256((row['conversation']+str(index)).encode()).hexdigest()[:12]
        snapshot={'logged_in':True,'identity_verified':True,'identity_source':'platform_dom','conversation_id':conversation,'input_text':'','messages':[]}
        transport=NoSendTransport(snapshot);service=TongchengService(out/('case-'+str(index)+'.sqlite'),transport,lambda:faqs)
        service.active=True;service.policy('replay','shadow');service.knowledge_scopes=lambda:scopes
        service.shadow_documents=lambda:json.loads((PRIVATE/'kdocs-knowledge.json').read_text('utf-8')).get('documents',[]) if (PRIVATE/'kdocs-knowledge.json').exists() else []
        service.ingest('replay',snapshot)
        snapshot['messages']=[{'id':row['id'],'direction':'inbound','kind':'text','text':row['text']}]
        service.ingest('replay',snapshot)
        task=service.db.execute('SELECT id FROM tasks').fetchone()[0]
        try:result=await service.generate_task(task,config)
        except ValueError as exc:result={'status':str(exc)}
        fresh=service.db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
        blocked=None
        if fresh['text']:
            receipt=await service.send_message(OutboundMessageCommand('replay',conversation,{'type':'text','text':fresh['text']},task))
            blocked=receipt.error=='shadow_mode_no_send'
        simulations=service.overview()['simulations']
        results.append({'source':'real_history_replay','question':row['text'],'status':result['status'],
            'simulated_reply':simulations[0]['text'] if simulations else '',
            'evidence':json.loads(simulations[0]['evidence']) if simulations else [],
            'send_blocked':blocked,'transport_send_calls':transport.calls})
        service.db.close()
    source.close()
    report={'at':datetime.now(timezone.utc).isoformat(),'kind':'isolated_real_history_replay','production_messages_written':False,
        'real_send_attempted':False,'cases':results}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    cards=[]
    for item in results:
        cards.append('<article><h2>真实历史问题</h2><p>'+html.escape(item['question'])+'</p><h3>模拟回复 · 未发送</h3><p>'+html.escape(item['simulated_reply'] or item['status'])+'</p><small>网络发送调用：'+str(item['transport_send_calls'])+'；发送拦截：'+str(item['send_blocked'])+'</small></article>')
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>同程影子回放测试</title><style>body{font:16px/1.7 system-ui;max-width:850px;margin:40px auto;background:#f4f6f9;color:#24364a}article{background:white;padding:24px;margin:20px 0;border:1px solid #dce3eb;border-radius:10px}p{white-space:pre-wrap}small{color:#68798b}</style><h1>同程影子回放测试</h1><p>使用真实历史问题，在隔离库执行。没有实时新咨询或同程发送；不写入生产聊天记录。</p>'+''.join(cards),'utf-8')
    (PRIVATE/'shadow-replay-latest.json').write_text(json.dumps({'report':str(out/'report.html'),'cases':len(results),'statuses':[r['status'] for r in results]},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'report':str(out/'report.html'),'cases':len(results),'statuses':[r['status'] for r in results],
        'transport_send_calls':sum(r['transport_send_calls'] for r in results),'production_messages_written':False},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
