"""Per-user scheduled supervisor. Restores the user-authorized reception mode; never sends messages itself."""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'artifacts/tongcheng-private'


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)


def should_resume(control, live):
    return (control.get('monitor_enabled') is True and live.get('mode') == 'shadow'
            and not live.get('active'))


class LocalClient:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''

    def call(self, path, body=None, timeout=5):
        request = Request('http://127.0.0.1:8791' + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf})
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def login(self):
        self.csrf = self.call('/auth/login', json.loads((PRIVATE / 'operator-login.json').read_text('utf-8')))['csrf']


class Supervisor:
    def __init__(self):
        self.child = None
        self.handles = []
        self.client = LocalClient()
        self.failures = 0
        self.restarts = 0
        self.next_start = 0
        self.next_resume = 0
        self.last_exit = None
        self.startup_until = 0
        self.last_live = {}

    def event(self, action, **fields):
        # Deliberately exclude exception text, command lines, messages and credentials.
        path = PRIVATE / 'resident-events.jsonl'
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix('.previous.jsonl'))
        with path.open('a', encoding='utf-8') as log:
            log.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'action': action, **fields}) + '\n')

    def launch(self):
        network_path=PRIVATE/'network.json'
        network=json.loads(network_path.read_text('utf-8')) if network_path.exists() else {}
        bind_host='0.0.0.0' if network.get('lan_enabled') is True else '127.0.0.1'
        for handle in self.handles:
            handle.close()
        self.handles = []
        for name in ('service.log', 'service-error.log'):
            path = PRIVATE / name
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(path.with_suffix('.previous.log'))
            self.handles.append(path.open('a', encoding='utf-8'))
        self.child = subprocess.Popen(
            [str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable, '-X', 'faulthandler', '-c',
             "import faulthandler; faulthandler.dump_traceback_later(20); import uvicorn; uvicorn.run('tongcheng_app:app',app_dir='server',host="+repr(bind_host)+",port=8791,access_log=False)"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=self.handles[0], stderr=self.handles[1],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.client = LocalClient()
        self.startup_until = time.time() + 360
        self.last_live = {}
        self.failures = 0
        self.next_resume = 0
        self.event('service_started', pid=self.child.pid)

    def tick(self):
        now = time.time()
        if self.child is None:
            if now >= self.next_start:
                self.launch()
            return
        code = self.child.poll()
        if code is not None:
            self.last_exit = {'at': datetime.now(timezone.utc).isoformat(), 'pid': self.child.pid, 'code': code}
            self.event('service_exited', **self.last_exit)
            self.child = None
            self.restarts += 1
            self.next_start = now + min(60, 2 ** min(self.restarts, 6))
            return
        try:
            if not self.client.csrf:
                self.client.login()
            live = self.client.call('/health/resident', timeout=12)
            self.failures = 0
            self.last_live = live
        except Exception as exc:
            self.client.csrf = ''
            self.failures += 1
            self.write_health('starting' if now < self.startup_until else 'unhealthy')
            if self.failures >= 6 and now >= self.startup_until:
                self.event('local_health_failed', error_type=type(exc).__name__, pid=self.child.pid)
                # Only terminate the child created by this supervisor, never an adopted PID.
                self.child.terminate()
            return
        control = json.loads((PRIVATE / 'resident-control.json').read_text('utf-8'))
        if should_resume(control, live) and now >= self.next_resume:
            self.next_resume = now + 60
            try:
                self.client.call('/open', {}, timeout=30)
                # Read policy/control again after login, so a concurrent pause wins.
                current = self.client.call('/health/resident', timeout=12)
                control = json.loads((PRIVATE / 'resident-control.json').read_text('utf-8'))
                if should_resume(control, current):
                    self.client.call('/start', {}, timeout=30)
                    self.event('shadow_monitor_resumed')
                    live = self.client.call('/health/resident', timeout=12)
            except Exception as exc:
                self.event('shadow_resume_waiting', error_type=type(exc).__name__)
        intent_path=PRIVATE/'production-intent.json'
        intent=json.loads(intent_path.read_text('utf-8')) if intent_path.exists() else {}
        if intent.get('enabled') is True and control.get('monitor_enabled') and live.get('mode')!='auto' and now>=self.next_resume:
            self.next_resume=now+60
            try:
                result=self.client.call('/production/resume',{},timeout=45)
                if result.get('status')=='auto_resumed':self.event('production_monitor_resumed')
                live=self.client.call('/health/resident', timeout=12)
            except Exception as exc:self.event('production_resume_waiting',error_type=type(exc).__name__)
        self.last_live = live
        self.write_health('healthy')

    def write_health(self, state):
        live = self.last_live
        control = json.loads((PRIVATE / 'resident-control.json').read_text('utf-8'))
        save(PRIVATE / 'resident-status.json', {
            'at': datetime.now(timezone.utc).isoformat(), 'supervisor_pid': os.getpid(),
            'health_state': state, 'consecutive_health_failures': self.failures,
            'service_pid': self.child.pid, 'restart_count': self.restarts, 'last_exit': self.last_exit,
            'mode': live.get('mode'), 'active': live.get('active'),
            'monitor_enabled': control.get('monitor_enabled'),
            'online': live.get('transport', {}).get('online'),
            'last_packet_at': live.get('transport', {}).get('last_packet_at')})

    def run(self):
        self.event('supervisor_started', pid=os.getpid())
        while True:
            try:
                self.tick()
            except Exception as exc:
                self.event('supervisor_tick_failed', error_type=type(exc).__name__)
            time.sleep(5)


def main():
    import msvcrt
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with (PRIVATE / 'resident-owner.lock').open('a+b') as owner:
        owner.seek(0)
        if not owner.read(1):
            owner.write(b'1'); owner.flush()
        owner.seek(0)
        try:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return  # Scheduler/launcher overlap must not create another socket owner.
        Supervisor().run()


if __name__ == '__main__':
    main()
