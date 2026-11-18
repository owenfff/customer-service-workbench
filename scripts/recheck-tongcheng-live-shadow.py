"""Re-evaluate received live questions with verified cards; never send or edit tasks."""
import argparse
import http.cookiejar
import json
import sqlite3
import sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler,HTTPCookieProcessor
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from workbench_core.shadow_documents import shadow_draft
from workbench_core.contextual_knowledge import merged_question
from workbench_core.knowledge import ModelConfig

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--message-id');args=parser.parse_args()
    private=ROOT/'artifacts/tongcheng-private';opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(path,data=None):
        with opener.open(Request('http://127.0.0.1:8791'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'}),timeout=20) as r:return json.load(r)
    call('/auth/login',json.loads((private/'operator-login.json').read_text('utf-8')))
    live=call('/shadow/live/data')
    if live['mode']!='shadow':raise ValueError('shadow_mode_required')
    config=ModelConfig(**json.loads((private/'model.json').read_text('utf-8')))
    knowledge=json.loads((private/'kdocs-knowledge.json').read_text('utf-8'))
    db=sqlite3.connect((private/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    report={'kind':'received_live_questions_rechecked_with_product','source_sha256':knowledge['source_sha256'],'model':config.model,'platform_send_calls':0,'results':[]}
    if args.message_id and (private/'live-shadow-rechecked.json').exists():
        previous=json.loads((private/'live-shadow-rechecked.json').read_text('utf-8'))
        if previous.get('model')==config.model and previous.get('source_sha256')==knowledge['source_sha256']:
            report['results']=[r for r in previous.get('results',[]) if r['message_id']!=args.message_id]
    for item in live['items']:
        if args.message_id and item['id']!=args.message_id:continue
        if item['kind']!='text' or not item['products']:continue
        conversation='ws:'+item['customer']
        history=[dict(r) for r in db.execute('SELECT * FROM messages WHERE account=? AND conversation=? AND rowid<=(SELECT rowid FROM messages WHERE account=? AND conversation=? AND id=?) ORDER BY rowid DESC LIMIT 20',('local',conversation,'local',conversation,item['id']))][::-1]
        question=merged_question(history)
        if question is None:continue
        result=shadow_draft(question,history,knowledge['documents'],config,product=item['products'],allow_clarification=True)
        report['results'].append({'customer':item['customer'],'message_id':item['id'],'question':item['text'],'received_at':item['received_at'],'at':datetime.now(timezone.utc).isoformat(),'products':item['products'],'product_context_source':item.get('product_context_source','preceding_card'),'product_context_message_id':item.get('product_context_message_id'),'status':result['status'],'text':result.get('text',''),'evidence':result.get('evidence',[]),'recomputed':True})
        target=private/'live-shadow-rechecked.json';temp=target.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');temp.replace(target)
        print(json.dumps({'question':item['text'],'status':result['status'],'reply':result.get('text','')},ensure_ascii=False),flush=True)
    db.close()

if __name__=='__main__':main()
