"""Stage existing Xianyu documents for explicit Tongcheng review, never activate."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]

def collect(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    items=[]
    try:
        for table in ('rag_documents','shared_rag_documents'):
            for row in db.execute('SELECT * FROM '+table):
                source=dict(row)
                key=hashlib.sha256(json.dumps([table,source.get('cookie_id'),source['id']]).encode()).hexdigest()
                content=source.get('content') or ''
                items.append({'id':key,'title':source.get('title') or '未命名资料','content':content,
                    'source_platform':'xianyu','source_type':source.get('source_type'),'source_table':table,
                    'original_item_id':str(source.get('item_id') or ''),'source_enabled':bool(source.get('enabled')),
                    'original_valid_until':source.get('valid_until'),'content_hash':hashlib.sha256(content.encode()).hexdigest(),
                    'reviewed':False,'enabled_for_tongcheng':False})
        return items
    finally:db.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    items=collect(ROOT.parent/'xianyu-auto-reply-fix/data/xianyu_data.db')
    if args.apply:
        path=ROOT/'artifacts/tongcheng-private/knowledge-inbox.json';path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(items,ensure_ascii=False,indent=2),'utf-8');temp.replace(path)
    print(json.dumps({'documents':len(items),'applied':args.apply,'auto_approved':0,'source_database_modified':False}))

if __name__=='__main__':main()
