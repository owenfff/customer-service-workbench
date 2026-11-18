"""Private Feishu settings and durable, at-most-once handoff notifications."""
import asyncio
import json
import sqlite3
from datetime import datetime,timezone,timedelta
from workbench_core.feishu_notification import build_request,send
from tongcheng_message_times import conversation_messages


def beijing(value):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')+' 北京时间'
    except (ValueError,TypeError,AttributeError):return '时间未知'


class FeishuNotifications:
    def __init__(self,private,service,transport,sender=send):
        self.private,self.service,self.transport,self.sender=private,service,transport,sender
        self.db=sqlite3.connect(str(private/'feishu-notifications.sqlite'),check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("CREATE TABLE IF NOT EXISTS deliveries(task TEXT PRIMARY KEY,status TEXT,reason TEXT,at TEXT); CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY,cutoff INTEGER);")
        self.db.execute("UPDATE deliveries SET status='uncertain',reason='interrupted_no_retry' WHERE status='sending'")
        self.db.commit()

    def config(self):
        path=self.private/'feishu-notifications.json'
        try:return json.loads(path.read_text('utf-8')) if path.exists() else {}
        except (OSError,ValueError):return {}

    def status(self):
        config=self.config();last=self.db.execute('SELECT status,reason,at FROM deliveries ORDER BY rowid DESC LIMIT 1').fetchone()
        return {'configured':bool(config.get('webhook')),'enabled':bool(config.get('enabled')),'has_secret':bool(config.get('secret')),
                'last_delivery':dict(last) if last else None,'delivered':self.db.execute("SELECT count(*) FROM deliveries WHERE status='accepted'").fetchone()[0]}

    def save(self,enabled,webhook=None,secret=None):
        previous=self.config()
        config={'enabled':bool(enabled),'webhook':webhook or previous.get('webhook',''),'secret':previous.get('secret','') if secret is None else secret}
        if config['webhook']:build_request(config['webhook'],'同程客服配置验证',secret=config['secret'])
        elif enabled:raise ValueError('feishu_webhook_required')
        # Saving starts a new notification window; never flood a new group with backlog.
        cutoff=self.service.db.execute('SELECT coalesce(max(rowid),0) FROM tasks').fetchone()[0]
        path=self.private/'feishu-notifications.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8');temp.replace(path)
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(cutoff,));self.db.commit()
        return self.status()

    async def test(self):
        config=self.config()
        if not config.get('webhook'):raise ValueError('feishu_webhook_required')
        text='同程客服 · 人工提醒测试\n'+beijing(datetime.now(timezone.utc).isoformat())+'\n收到此消息即表示飞书机器人通道正常。请在手机飞书检查该群的消息通知设置。'
        return await asyncio.to_thread(self.sender,config['webhook'],text,secret=config.get('secret'))

    def text(self,task):
        row=self.service.db.execute('SELECT text FROM messages WHERE account=? AND conversation=? AND id=?',(task['account'],task['conversation'],task['message'])).fetchone()
        meta=self.service.db.execute('SELECT label FROM conversation_meta WHERE account=? AND conversation=?',(task['account'],task['conversation'])).fetchone()
        message=next((m for m in conversation_messages(self.service,self.transport,task['conversation']) if m['id']==task['message']),{})
        reasons={'requires_human':'资料不足或涉及需人工确认的业务条件','source_insufficient':'产品资料缺少对应规则','human_takeover':'会话已由人工接管','unsupported_message':'消息内容无法可靠识别','model_error':'模型调用失败','model_unconfigured':'模型尚未配置','shadow_model_or_citation_error':'模型或知识引用校验未通过','queue_full_requires_human':'自动处理队列已满'}
        case=self.service.db.execute('SELECT reason FROM handoff_cases WHERE task=?',(task['id'],)).fetchone()
        original_reason=case[0] if case else task['reason']
        reason='发送结果不确定，请核对同程，勿重复发送' if task['status']=='uncertain' else reasons.get(original_reason,'自动处理未完成，请人工核对')
        return '\n'.join(['同程客服 · 待人工处理','客户：'+((meta[0] if meta else task['conversation'])[:100]),'收到：'+beijing(message.get('display_time')),
                          '客户原话：'+((row[0] if row else '[消息不可用]')[:600]),'原因：'+reason,
                          '请回电脑工作台人工接管后回复；处理完可交还 AI。'])

    async def tick(self):
        config=self.config()
        if not config.get('enabled') or not config.get('webhook'):return
        mode=self.service.db.execute("SELECT mode FROM policies WHERE account='local'").fetchone()
        if not self.service.active or not mode or mode[0]!='auto':return
        cutoff=self.db.execute('SELECT cutoff FROM settings WHERE id=1').fetchone()
        if not cutoff:return
        tasks=self.service.db.execute("SELECT t.* FROM tasks t JOIN messages m ON m.account=t.account AND m.conversation=t.conversation AND m.id=t.message WHERE t.account='local' AND t.rowid>? AND t.conversation LIKE 'ws:%' AND m.direction='inbound' AND m.kind='text' AND (EXISTS(SELECT 1 FROM handoff_cases h WHERE h.task=t.id AND h.status='open') OR t.status='uncertain' OR (t.status='human_review' AND t.reason NOT IN ('human_or_knowledge_required','human_takeover'))) ORDER BY t.rowid LIMIT 100",(cutoff[0],)).fetchall()
        for task in tasks:
            if self.db.execute('SELECT 1 FROM deliveries WHERE task=?',(task['id'],)).fetchone():continue
            # Skip a stale item that has already been superseded or taken over.
            if task['status']!='uncertain' and self.service.is_human(task['account'],task['conversation']):continue
            now=datetime.now(timezone.utc).isoformat()
            self.db.execute("INSERT INTO deliveries VALUES(?,'sending','dispatching',?)",(task['id'],now));self.db.commit()
            try:result=await asyncio.to_thread(self.sender,config['webhook'],self.text(task),secret=config.get('secret'))
            except Exception:result={'ok':False,'reason':'transport_failed'}
            state='accepted' if result.get('ok') else 'uncertain' if result.get('reason')=='transport_failed' else 'failed'
            self.db.execute('UPDATE deliveries SET status=?,reason=?,at=? WHERE task=?',(state,result.get('reason','unknown'),datetime.now(timezone.utc).isoformat(),task['id']));self.db.commit()

    async def run(self):
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:
                # No exception text, URL, signature, customer text or credentials in logs.
                pass
            await asyncio.sleep(3)

    def close(self):self.db.close()
