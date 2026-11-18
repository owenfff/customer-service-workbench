"""Preview or import an exported KDocs workbook into local private shadow sources."""
import argparse
import json
import shutil
import sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from workbench_core.workbook_knowledge import extract_workbook

def main():
    parser=argparse.ArgumentParser();parser.add_argument('file',type=Path);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();source=args.file.resolve()
    result=extract_workbook(source,'https://www.kdocs.cn/l/ceXQXvul44EQ')
    result['imported_at']=datetime.now(timezone.utc).isoformat()
    if args.apply:
        private=ROOT/'artifacts/tongcheng-private';raw=private/'knowledge-sources';raw.mkdir(parents=True,exist_ok=True)
        copy=raw/(result['source_sha256']+'.xlsx')
        if not copy.exists():shutil.copy2(source,copy)
        result['source_file']=str(copy)
        path=private/'kdocs-knowledge.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');temp.replace(path)
    print(json.dumps({'applied':args.apply,'sheets':result['sheets'],'documents':len(result['documents'])},ensure_ascii=False))

if __name__=='__main__':main()
