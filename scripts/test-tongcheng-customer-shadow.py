"""Replay received customer questions with their scoped context; never platform send."""
import asyncio
import argparse
import hashlib
import html
import json
import sqlite3
import sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'server'))
from tongcheng import TongchengService
from contracts import OutboundMessageCommand
from workbench_core.knowledge import ModelConfig,FAQ

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--retry-failed',action='store_true');args=parser.parse_args()
    private=ROOT/'artifacts/tongcheng-private'
    source=sqlite3.connect((private/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True);source.row_factory=sqlite3.Row
    docs=json.loads((private/'kdocs-knowledge.json').read_text('utf-8'))
    faqs=[FAQ.model_validate(f) for f in json.loads((private/'knowledge.json').read_text('utf-8'))]
    config=ModelConfig(**json.loads((private/'model.json').read_text('utf-8')))
    previous={}
    if args.retry_failed and (private/'customer-shadow-latest.json').exists():
        old_path=Path(json.loads((private/'customer-shadow-latest.json').read_text('utf-8'))['report']).with_suffix('.json')
        old=json.loads(old_path.read_text('utf-8'))
        if old.get('source_sha256')==docs['source_sha256'] and old.get('model')==config.model:previous={r['question']:r for r in old['results']}
    # Exact original questions, selected to cover different business issues.
    questions=['你好，我想咨询几个套餐主要区别是什么？','你好 我的这个订单能开发票吗？','你好 明天的迪士尼乐园不去的话费用能退吗？天气太热了，孩子不想去了',
        '这条线路有什么独家亮点 / 特色？','坐船有吗','周庄里面有什么，需要其它费用吗','酒店是几个人住','10个月婴儿可以去买']
    out=private/'customer-shadow-tests'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');out.mkdir(parents=True,exist_ok=True)
    results=[]
    for index,question in enumerate(questions):
        earlier=previous.get(question)
        if earlier and earlier['status'] in ('shadow_draft_ready','shadow_clarification_ready'):
            results.append(earlier);continue
        row=source.execute("SELECT rowid,* FROM messages WHERE direction='inbound' AND kind='text' AND text=? ORDER BY rowid DESC LIMIT 1",(question,)).fetchone()
        if not row:continue
        history=[dict(m) for m in source.execute('SELECT * FROM (SELECT rowid,* FROM messages WHERE account=? AND conversation=? AND rowid<=? ORDER BY rowid DESC LIMIT 12) ORDER BY rowid',(row['account'],row['conversation'],row['rowid']))]
        product=source.execute('SELECT payload FROM product_context WHERE account=? AND conversation=?',(row['account'],row['conversation'])).fetchone()
        products=json.loads(product[0]) if product else []
        class NoSend:
            calls=0
            async def send_checked(self,*args):self.calls+=1;raise AssertionError('customer_replay_never_sends')
        transport=NoSend();service=TongchengService(out/(str(index)+'.sqlite'),transport,lambda:faqs)
        service.active=True;service.policy('test','shadow');service.shadow_documents=lambda:docs['documents']
        conversation='replay:'+hashlib.sha256((row['account']+row['conversation']).encode()).hexdigest()[:12]
        snap={'logged_in':True,'identity_verified':True,'identity_source':'platform_dom','conversation_id':conversation,'products':products,'messages':history[:-1]}
        service.ingest('test',snap);snap['messages']=history;service.ingest('test',snap)
        # Historical merchant messages establish a baseline only, not live handoff.
        task=service.db.execute('SELECT id FROM tasks WHERE message=?',(row['id'],)).fetchone()[0]
        try:status=await service.generate_task(task,config)
        except ValueError as exc:status={'status':str(exc)}
        simulation=service.overview()['simulations'][0] if service.overview()['simulations'] else {}
        answer=simulation.get('text','')
        blocked=None
        if answer:
            service.edit_task(task,answer)
            receipt=await service.send_message(OutboundMessageCommand('test',conversation,{'type':'text','text':answer},task))
            blocked=receipt.error=='shadow_mode_no_send'
        results.append({'number':index+1,'source':'received_real_customer_message_replayed','question':question,
            'source_message_hash':hashlib.sha256(row['id'].encode()).hexdigest(),'context_messages':len(history),
            'product_hint':[{'id':p['external_product_id'],'title':p['title']} for p in products],
            'product_hint_timing':'stored_conversation_context_not_historical_snapshot',
            'status':status['status'],'initial_status':earlier.get('initial_status',earlier['status']) if earlier else status['status'],'failure_reason':status.get('reason'),'reply':answer,'evidence':json.loads(simulation.get('evidence','[]')),
            'send_blocked':blocked,'platform_send_calls':transport.calls})
        service.db.close()
    source.close()
    summary={'at':datetime.now(timezone.utc).isoformat(),'source_sha256':docs['source_sha256'],'model':config.model,
        'kind':'received_customer_history_replay','production_messages_written':False,'platform_send_calls':0,'results':results}
    (out/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
    cards=[]
    for r in results:
        label='知识库模拟回复' if r['evidence'] else '信息不足的核实 / 追问建议'
        body=r['reply'] or '没有生成通过校验的文案，需人工处理（'+r['status']+'）。'
        refs='\n'.join(e.get('sheet','审核问答')+' · '+str(e.get('row',''))+'行：'+e.get('quote','') for e in r['evidence'])
        cards.append('<article><h2>客户问题 '+str(r['number'])+'</h2><blockquote>'+html.escape(r['question'])+'</blockquote><h3>'+label+' · 未发送</h3><p>'+html.escape(body)+'</p>'+
            ('<details><summary>查看知识依据</summary><pre>'+html.escape(refs)+'</pre></details>' if refs else '<small>无充分资料支持，不能作为已核实的产品结论。</small>')+
            '<p class="meta">网络发送次数：'+str(r['platform_send_calls'])+'；保留同一会话上下文 '+str(r['context_messages'])+' 条。</p></article>')
    report=out/'report.html';report.write_text('<!doctype html><meta charset="utf-8"><title>真实客户问题与模拟回复</title><style>body{font:16px/1.7 system-ui;max-width:900px;margin:40px auto;padding:0 20px;background:#f4f6f9;color:#24364a}article{background:white;padding:24px;margin:20px 0;border:1px solid #dce3eb;border-radius:10px}p,pre{white-space:pre-wrap}blockquote{margin:0;padding:14px;background:#eef4fd;border-left:3px solid #286cd6}.meta,small{color:#68798b}</style><h1>真实客户问题与模拟回复</h1><p>使用已接收的真实客户问题和同一会话上下文，结合当前同程产品库重新生成；这是隔离回放，不是实时新消息或当时的原始回复。商品信息使用当前保存的会话线索，不冒充提问当时的商品快照。向同程发送0条，未修改生产聊天记录。</p>'+''.join(cards),'utf-8')
    (private/'customer-shadow-latest.json').write_text(json.dumps({'report':str(report),'cases':len(results)},ensure_ascii=False),'utf-8')
    print(json.dumps({'report':str(report),'cases':len(results),'statuses':[r['status'] for r in results],'platform_send_calls':sum(r['platform_send_calls'] for r in results)},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
