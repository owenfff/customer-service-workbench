"""Authenticated local smoke check; never generates or sends customer replies."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import time
from urllib.request import Request,build_opener,ProxyHandler,HTTPCookieProcessor

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT/'artifacts/tongcheng-private'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--open',action='store_true');parser.add_argument('--start',action='store_true')
    args=parser.parse_args()
    opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=''
    def call(path,body=None):
        request=Request('http://127.0.0.1:8791'+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
        with opener.open(request,timeout=40) as response:return json.load(response)
    csrf=call('/auth/login',json.loads((PRIVATE/'operator-login.json').read_text('utf-8')))['csrf']
    if args.open:call('/open',{})
    probe=None
    for _ in range(12):
        try:
            probe=call('/probe',{})
            if probe.get('logged_in'):break
        except Exception:pass
        time.sleep(.5)
    if args.start and probe and probe.get('identity_verified'):call('/start',{})
    state=call('/state');conversations=call('/conversations')['items']
    report={'probe':probe,'state':state['state'],'last_error':state.get('last_error'),'active':state['active'],
        'conversations':len(conversations),'messages':sum(c['message_count'] for c in conversations),
        'tasks':len(state['tasks']),'model':call('/model'),'customer_send_tested':False}
    (PRIVATE/'smoke-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
