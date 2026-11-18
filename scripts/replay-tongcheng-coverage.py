"""Replay received failures in an isolated database. Never connect or send."""
import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT))
from tongcheng import TongchengService
from tongcheng_ownership import classify_outbound,PASSIVE_ORIGINS


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--since',default='2026-10-28T10:00:00+00:00');parser.add_argument('--model',action='store_true',help='Check eligible drafts using configured model; never send platform messages');args=parser.parse_args()
    private=ROOT/'artifacts/tongcheng-private'
    state=sqlite3.connect((private/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True);state.row_factory=sqlite3.Row
    wire=sqlite3.connect((private/'wire-inbox.sqlite').resolve().as_uri()+'?mode=ro',uri=True);wire.row_factory=sqlite3.Row
    cases=state.execute("SELECT a.id,a.at,t.id AS task_id,t.conversation,t.message,m.text,m.kind FROM audit a JOIN tasks t ON a.entity=t.id JOIN messages m ON t.account=m.account AND t.conversation=m.conversation AND t.message=m.id WHERE a.action='reply_generation_failed' AND a.at>=? ORDER BY a.id",(args.since,)).fetchall()
    results=[]
    for case in cases:
        customer=case['conversation'].removeprefix('ws:')
        timeline=[dict(r,event='message') for r in wire.execute('SELECT i.*,o.received_at,o.message_type,o.content_sample,p.payload AS products FROM inbox i JOIN observations o ON i.customer=o.customer AND i.id=o.id LEFT JOIN product_cards p ON p.customer=i.customer AND p.id=i.id WHERE i.customer=? AND o.received_at<=? ORDER BY o.received_at',(customer,case['at']))]
        timeline += [dict(r,event='control',received_at=r['at']) for r in state.execute("SELECT at,action FROM audit WHERE entity=? AND action IN ('human_takeover','return_to_ai') AND at<=?",(case['conversation'],case['at']))]
        timeline.sort(key=lambda row:row['received_at'])
        with tempfile.TemporaryDirectory(dir=private,prefix='coverage-replay-') as folder:
            service=TongchengService(Path(folder)/'isolated.sqlite');service.active=True
            service.db.execute("INSERT INTO policies VALUES('local','auto','[]')");service.db.commit()
            snap={'logged_in':True,'identity_verified':True,'identity_source':'platform_websocket','conversation_id':case['conversation'],'messages':[]}
            service.ingest('local',snap);outbound=[]
            try:
                for row in timeline:
                    if row['event']=='control':service.handoff('local',case['conversation'],row['action']=='human_takeover');continue
                    origin=classify_outbound(row['kind'],row['text'],row['message_type'],row['content_sample']) if row['direction']=='outbound' else None
                    message={'id':row['id'],'kind':row['kind'],'text':row['text'],'direction':row['direction'],'newly_received':True,'outbound_origin':origin,'received_at':row['received_at'],'occurred_at':row['at'],'products':json.loads(row['products']) if row['products'] else []}
                    if row['products']:snap['products']=message['products']
                    snap['messages'].append(message);service.ingest('local',snap)
                    if origin:outbound.append({'message_id':row['id'],'classification':origin,'received_at':row['received_at']})
                task=service.db.execute('SELECT status,reason FROM tasks WHERE message=?',(case['message'],)).fetchone()
                human=service.is_human('local',case['conversation'])
                outcome='attachment_not_a_text_question' if case['kind']!='text' else 'merchant_business_reply_protected' if human else 'superseded_by_new_message' if task and task['status']=='cancelled' else 'ai_eligible_after_fix'
                generated=None;handling=None
                if args.model and outcome=='ai_eligible_after_fix':
                    from workbench_core.knowledge import ModelConfig
                    from tongcheng_model import provider,status as model_status
                    from tongcheng_production import load_intent,library_documents
                    config=ModelConfig(**json.loads((private/'model.json').read_text('utf-8')))
                    documents=json.loads((private/'kdocs-knowledge.json').read_text('utf-8'))['documents'];intent=load_intent(private)
                    service.shadow_documents=lambda:documents;service.authorized_library=lambda:library_documents(documents,intent)
                    service.production_authorized=lambda:True;service.production_revision=lambda:intent.get('revision');service.model_available=lambda:model_status(Path(folder),config).get('state')=='available'
                    candidate=service.db.execute('SELECT id FROM tasks WHERE message=?',(case['message'],)).fetchone()
                    try:
                        generated=asyncio.run(service.generate_task(candidate[0],config,provider=provider(Path(folder),config)))
                        draft=service.db.execute('SELECT text,evidence,source FROM tasks WHERE id=?',(candidate[0],)).fetchone()
                        generated=dict(generated,text=draft['text'],evidence=json.loads(draft['evidence']),source=draft['source'])
                        if generated['status']!='draft_ready':
                            ready=service.prepare_handoff_notice(candidate[0],generated.get('reason') or generated['status'])
                            handling={'status':'human_queue_notice_ready' if ready else 'human_queue_only','business_answered':False}
                        else:handling={'status':'grounded_or_clarification_ready','business_answered':generated['source']=='documents_model'}
                    except Exception as exc:generated={'status':'generation_stopped','reason':str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else type(exc).__name__}
                results.append({'original_task':case['task_id'],'customer':customer,'message_id':case['message'],'question':case['text'],'original_failure_at':case['at'],
                                'outcome':outcome,'outbound_evidence':outbound,'replay_human_owned':human,'replay_task':dict(task) if task else None,'model_check':generated,'handling':handling})
            finally:service.db.close()
    report={'created_at':datetime.now(timezone.utc).isoformat(),'kind':'isolated_historical_replay','live_database_modified':False,'platform_send_calls':0,'cases':len(results),
            'summary':dict(Counter(row['outcome'] for row in results)),'model_verification':dict(Counter(row['model_check']['status'] for row in results if row['model_check'])),'handling_verification':dict(Counter(row['handling']['status'] for row in results if row['handling'])),'results':results}
    path=private/'coverage-12-replay.json';temp=path.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');temp.replace(path)
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))


if __name__=='__main__':main()
