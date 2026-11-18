"""Source-derived validation questions in isolated databases. No platform transport."""
import asyncio
import html
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'server'))
from tongcheng import TongchengService
from contracts import OutboundMessageCommand
from workbench_core.knowledge import ModelConfig

async def main():
    private=ROOT/'artifacts/tongcheng-private'
    source=json.loads((private/'kdocs-knowledge.json').read_text('utf-8'))
    config=ModelConfig(**json.loads((private/'model.json').read_text('utf-8')))
    out=private/'knowledge-tests'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');out.mkdir(parents=True,exist_ok=True)
    results=[]
    for index,question in enumerate(['同程产品1888072的三天行程是怎样安排的？','拈花湾儿童费用如何收取？','总统府的退票价格是多少？']):
        class NoSend:
            calls=0
            async def send_checked(self,*args):self.calls+=1;raise AssertionError('never_send')
        transport=NoSend();service=TongchengService(out/(str(index)+'.sqlite'),transport)
        service.active=True;service.policy('test','shadow');service.shadow_documents=lambda:source['documents']
        snap={'logged_in':True,'identity_verified':True,'identity_source':'platform_dom','conversation_id':'isolated-source-test','messages':[]}
        service.ingest('test',snap);snap['messages']=[{'id':'source-test-question','direction':'inbound','kind':'text','text':question}];service.ingest('test',snap)
        task=service.db.execute('SELECT id FROM tasks').fetchone()[0]
        generated=await service.generate_task(task,config)
        simulation=service.overview()['simulations'][0] if service.overview()['simulations'] else {}
        blocked=await service.send_message(OutboundMessageCommand('test','isolated-source-test',{'type':'text','text':''},task))
        results.append({'question':question,'kind':'source_derived_validation_question','status':generated['status'],'reply':simulation.get('text',''),
            'evidence':json.loads(simulation.get('evidence','[]')),'send_blocked':not blocked.accepted,'network_send_calls':transport.calls})
        service.db.close()
    report={'source':'同程旅行产品库.xlsx','source_sha256':source['source_sha256'],'real_customer_messages':False,'platform_sends':0,'results':results}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    cards=[]
    for r in results:
        refs='\n'.join(ref['sheet']+' · 第'+str(ref['row'])+'行：'+ref['quote'] for ref in r['evidence'])
        cards.append('<article><h2>'+html.escape(r['question'])+'</h2><b>模拟回复 · 未发送</b><p>'+html.escape(r['reply'] or r['status'])+'</p><pre>'+html.escape(refs)+'</pre><small>发送拦截：'+str(r['send_blocked'])+'；网络发送次数：'+str(r['network_send_calls'])+'</small></article>')
    report_path=out/'report.html';report_path.write_text('<!doctype html><meta charset="utf-8"><title>同程知识库影子测试</title><style>body{font:16px/1.7 system-ui;max-width:880px;margin:40px auto;background:#f4f6f9;color:#24364a}article{background:white;padding:24px;margin:20px 0;border:1px solid #dce3eb;border-radius:10px}p,pre{white-space:pre-wrap}small{color:#68798b}</style><h1>同程产品库影子测试</h1><p>使用资料校验题及真实配置模型，在隔离库测试；不是实时客户咨询，向同程发送0条。价格与退费资料时效仍需人工核对。</p>'+''.join(cards),'utf-8')
    print(json.dumps({'report':str(report_path),'statuses':[r['status'] for r in results],'platform_sends':0},ensure_ascii=False))
    (private/'knowledge-test-latest.json').write_text(json.dumps({'report':str(report_path)},ensure_ascii=False),'utf-8')

if __name__=='__main__':asyncio.run(main())
