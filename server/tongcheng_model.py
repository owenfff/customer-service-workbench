"""Bounded model calls and private, redacted availability state."""
import hashlib
import json
import re
import threading
import time
import httpx
from workbench_core.knowledge import _quiet_http_logging, configure

_lock=threading.Lock()


def signature(config):
    return hashlib.sha256(json.dumps([config.endpoint,config.model,config.api_key.get_secret_value()]).encode()).hexdigest()


def status(private, config):
    path=private/'model-live-health.json'
    value=json.loads(path.read_text('utf-8')) if path.exists() else {}
    if value.get('signature')!=signature(config):return {'state':'not_checked'}
    return {k:v for k,v in value.items() if k!='signature'}


def provider(private, config, *, probe=False):
    def invoke(payload):
        if configure(config)['state']!='configured':raise ValueError('model_unconfigured')
        current=status(private,config)
        if not probe and current.get('retry_after',0)>time.time():raise ValueError('model_circuit_open')
        state={'signature':signature(config),'checked_at':time.time(),'model':config.model}
        try:
            with _quiet_http_logging(), httpx.Client(timeout=config.timeout_seconds,trust_env=False,follow_redirects=False) as client:
                with client.stream('POST',config.endpoint,json=payload,headers={'Authorization':'Bearer '+config.api_key.get_secret_value()}) as response:
                    data=bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data)>64000:raise ValueError('response_too_large')
                    state['http_status']=response.status_code
                    if response.status_code!=200:
                        try:code=json.loads(data).get('error',{}).get('code','')
                        except Exception:code=''
                        state.update(state='unavailable',error_code=code if isinstance(code,str) and re.fullmatch(r'[\w.]{1,100}',code) else 'model_http_error',retry_after=time.time()+(900 if response.status_code==429 else 60))
                        raise ValueError('model_http_error')
                    choice=json.loads(data)['choices'][0]
                    if choice.get('finish_reason')=='length':raise ValueError('model_output_truncated')
                    content=choice['message']['content']
                    state.update(state='available',retry_after=0)
                    return content
        except Exception as exc:
            if 'state' not in state:state.update(state='unavailable',error_code=type(exc).__name__,retry_after=time.time()+60)
            raise
        finally:
            with _lock:
                path=private/'model-live-health.json';temp=path.with_suffix('.tmp')
                temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),'utf-8');temp.replace(path)
    return invoke
