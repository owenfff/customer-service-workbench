"""Deliberate local service exit/pause acceptance. No platform send calls."""
import http.cookiejar
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

PRIVATE=Path(__file__).resolve().parents[1]/'artifacts/tongcheng-private'


def client():
    opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=''
    def call(path, body=None):
        with opener.open(Request('http://127.0.0.1:8791'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','X-CSRF-Token':csrf}), timeout=4) as r:
            return json.load(r)
    csrf=call('/auth/login',json.loads((PRIVATE/'operator-login.json').read_text('utf-8')))['csrf']
    return call


def terminate_owned_service():
    status=json.loads((PRIVATE/'resident-status.json').read_text('utf-8'))
    pid=int(status['service_pid']);parent=int(status['supervisor_pid'])
    command=f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if (-not $p -or $p.ParentProcessId -ne {parent} -or $p.CommandLine -notlike '*uvicorn tongcheng_app:app*') {{ throw 'Owner mismatch' }}; Stop-Process -Id {pid} -Force"
    subprocess.run(['powershell.exe','-NoProfile','-Command',command],check=True,capture_output=True)
    return pid


def wait_recovered(old_pid, active):
    start=time.monotonic()
    while time.monotonic()-start<55:
        try:
            call=client();live=call('/shadow/live/data')
            status=json.loads((PRIVATE/'resident-status.json').read_text('utf-8'))
            if status['service_pid']!=old_pid and live['mode']=='shadow' and live['active']==active and (not active or live['transport'].get('online')):
                return call,live,{'old_pid':old_pid,'new_pid':status['service_pid'],
                    'seconds':round(time.monotonic()-start,2),'active':live['active'],
                    'mode':live['mode'],'last_exit':status['last_exit']}
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('automatic_recovery_timeout')


def main():
    call=client();live=call('/shadow/live/data')
    if live['mode']!='shadow' or not live['active']:
        raise ValueError('active_shadow_required')
    report={'started_at':datetime.now(timezone.utc).isoformat(),'platform_send_calls':0,'checks':[]}
    old=terminate_owned_service()
    call,live,result=wait_recovered(old,True)
    report['checks'].append(dict(result,test='forced_service_exit_auto_shadow_recovery'))
    print(json.dumps(report['checks'][-1]),flush=True)
    call('/pause',{})
    try:
        time.sleep(12)
        assert not call('/shadow/live/data')['active']
        old=terminate_owned_service()
        call,live,result=wait_recovered(old,False)
        time.sleep(12)
        assert not call('/shadow/live/data')['active']
        report['checks'].append(dict(result,test='explicit_pause_survives_service_exit'))
        print(json.dumps(report['checks'][-1]),flush=True)
    finally:
        # Resume only the explicitly authorized shadow monitoring after the pause test.
        call=client()
        assert call('/shadow/live/data')['mode']=='shadow'
        call('/open',{})
        call('/start',{})
    report['completed_at']=datetime.now(timezone.utc).isoformat()
    (PRIVATE/'resident-recovery-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')


if __name__=='__main__':
    main()
