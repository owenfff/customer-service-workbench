"""Narrow platform-message exclusions; unknown merchant replies stay human-owned."""
import html
import json
import re


PASSIVE_ORIGINS=frozenset({'automatic_welcome','automatic_away','platform_question_card','coupon_card'})
WELCOME_TEXTS=frozenset({
    '尊敬的客人，您好！欢迎咨询华东旅行~',
    '亲亲您好，欢迎光临同程旅行核心供应商 南京万达国际旅行社有限公司，我社深耕华东旅游多年，我是您的专属私人顾问，请问有什么可以帮助您？',
})
AWAY_TEXT='这个信息需要商家确认哦～目前人工客服暂未上线，明天的 8 点后我们会为您转接专属服务人员，一有结果马上告诉你～'
HANDOFF_NOTICE='这个问题需要人工核对，已记录待处理。'


def normalize(text):
    return re.sub(r'\s+','',html.unescape(re.sub(r'<br\s*/?>','',text or '',flags=re.I)))


def decode_content(value):
    for _ in range(4):
        if not isinstance(value,str):break
        try:value=json.loads(value)
        except (ValueError,TypeError):
            # The observed SDK occasionally stores escaped JSON without outer quotes.
            if value.startswith('{\\"'):
                value=value.replace('\\"','"')
                continue
            return None
    return value if isinstance(value,dict) else None


def classify_outbound(kind,text,message_type=None,content=None):
    if kind=='text':
        normalized=normalize(text)
        if normalized in {normalize(t) for t in WELCOME_TEXTS}:return 'automatic_welcome'
        if normalized==normalize(AWAY_TEXT):return 'automatic_away'
    data=decode_content(content)
    if str(message_type)=='17' and data and data.get('bizType')=='ai_agent_questions' and isinstance(data.get('data'),list):return 'platform_question_card'
    if str(message_type)=='12' and data and data.get('iframeType')==2 and data.get('title')=='30元旅游度假红包' and data.get('usageRules')=='可用于旅游度假产品':return 'coupon_card'
    return 'merchant_reply'


def passive(message):
    return message.get('direction')=='outbound' and (message.get('outbound_origin') or classify_outbound(message.get('kind'),message.get('text',''),message.get('message_type'),message.get('content'))) in PASSIVE_ORIGINS


def relevant_messages(messages):return [m for m in messages if not passive(m)]


def customer_question(history):
    """An older attachment ends a text run; it must not discard newer text."""
    tail=[]
    for message in reversed(history):
        if message['direction']!='inbound' or message.get('kind')!='text':break
        tail.append(message.get('text',''))
        if len(tail)>=10:break
    text='\n'.join(reversed(tail))
    return text if tail and len(text)<=8000 else None


def repair_roles(service,transport):
    """Correct only proven passive holds; never reopen a task or send old replies."""
    if getattr(transport,'config',{}).get('transport')!='independent_websocket':return {'classified':0,'released':0}
    classified=0;released=0
    for row in transport.db.execute('SELECT i.customer,i.id,i.kind,i.text,o.message_type,o.content_sample FROM inbox i LEFT JOIN observations o ON i.customer=o.customer AND i.id=o.id WHERE i.direction="outbound"'):
        origin=classify_outbound(row['kind'],row['text'],row['message_type'],row['content_sample'])
        service.db.execute('INSERT OR REPLACE INTO message_origins VALUES(?,?,?,?)',('local','ws:'+row['customer'],row['id'],origin))
        classified+=int(origin in PASSIVE_ORIGINS)
    launch=service.db.execute("SELECT max(at) FROM audit WHERE action='production_user_authorized_launch'").fetchone()[0]
    for session in service.db.execute("SELECT * FROM sessions WHERE account='local' AND human=1").fetchall():
        explicit=service.db.execute("SELECT at,action FROM audit WHERE entity=? AND action IN ('human_takeover','return_to_ai') ORDER BY id DESC LIMIT 1",(session['conversation'],)).fetchone()
        if explicit and explicit['action']=='human_takeover':continue
        cutoff=max(filter(None,[launch,explicit['at'] if explicit else None]),default=None)
        if not cutoff:continue
        customer=session['conversation'].removeprefix('ws:')
        rows=transport.db.execute('SELECT i.kind,i.text,o.message_type,o.content_sample FROM inbox i LEFT JOIN observations o ON i.customer=o.customer AND i.id=o.id WHERE i.customer=? AND i.direction="outbound" AND coalesce(o.received_at,i.at,"")>?',(customer,cutoff)).fetchall()
        if rows and all(classify_outbound(r['kind'],r['text'],r['message_type'],r['content_sample']) in PASSIVE_ORIGINS for r in rows):
            service.db.execute('UPDATE sessions SET human=0 WHERE account=? AND conversation=?',('local',session['conversation']))
            service.audit('automatic_only_handoff_repaired',session['conversation']);released+=1
    service.db.commit()
    return {'classified':classified,'released':released}
