"""Version-bound user launch authority and non-factual clarification limits."""
import hashlib
import json
import re
from datetime import datetime, timezone
from workbench_core.shadow_documents import public_row


def load_intent(private):
    try:return json.loads((private/'production-intent.json').read_text('utf-8'))
    except (OSError,ValueError):return {}


def account_binding(transport):
    session=getattr(transport,'session',{})
    values=[str(session.get(k,'')) for k in ('source_uid','userId','spaceId','tenant_code')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest() if values[0] and values[1] else None


def valid_intent(intent):
    try:
        return bool(intent.get('enabled') is True and intent.get('authority')=='explicit_user_live_launch'
                    and datetime.now(timezone.utc)<datetime.fromisoformat(intent['expires_at']))
    except (ValueError,TypeError,KeyError):return False


def library_documents(documents,intent):
    if not valid_intent(intent):return []
    result=[]
    for doc in documents:
        row=public_row(doc)
        if row and row['product_ids'] and doc['source_sha256']==intent.get('source_sha256') and intent.get('rows',{}).get(doc['id'])==doc['content_hash']:
            result.append(dict(doc,approval={'kind':'user_authorized_library','revision':intent['revision'],
                'source_sha256':doc['source_sha256'],'content_hash':doc['content_hash'],'expires_at':intent['expires_at']}))
    return result


def clarification_slot(question,history,answer):
    """Only collect one missing condition. Never authorize a business conclusion."""
    clean=re.sub(r'<[^>]+>','',question).strip()
    if re.fullmatch(r'(你好|您好|在吗|你好呀|您好呀)[，。！!？?\s]*',clean) and answer in ('您好，有什么可以帮您？','您好，想了解哪方面呀？','您好，有什么可以帮您呀？'):
        return 'greeting'
    choices={
        'date':{'请问计划哪天出游呀？','您计划哪天出游？','请问计划哪天出游？','计划哪天出游呀？','您打算哪天出游呀？'},
        'people':{'一共几位出游呀？','请问一共几位出游？','您这边一共几位出游呀？'},
        'age':{'请问小朋友多大了？','小朋友多大了呀？','请问小朋友几岁呀？'},
    }
    slot=next((s for s,values in choices.items() if answer in values),None)
    if not slot:return None
    inbound='\n'.join(m.get('text','') for m in history if m['direction']=='inbound')
    outbound='\n'.join(m.get('text','') for m in history if m['direction']=='outbound')
    if any(a in outbound for a in choices[slot]):return None
    if slot=='people':
        return slot if re.search('优惠|多人|团购',question) and not re.search(r'\d+\s*[人位名]|[一两二三四五六七八九十]+个?人|[一两二三四五六七八九十]+大[一两二三四五六七八九十]+小',inbound) else None
    date_known=bool(re.search(r'\d{1,2}\s*[月号日]|今天|明天|后天|周[一二三四五六日天]|星期|本周|下周|[零〇一二两三四五六七八九十]{1,3}月[零〇一二两三四五六七八九十]{1,3}[号日]?',inbound))
    price=bool(re.search('价格|多少钱|费用|收费|报价',question))
    if slot=='date':return slot if price and not date_known else None
    if slot=='age':return slot if price and date_known and re.search('小孩|小朋友|儿童|孩子',question) and not re.search(r'\d+\s*岁|[一两二三四五六七八九十]+岁|身高|[一两二三四五六七八九十]+米[一两二三四五六七八九十]*|\d[.\d]*\s*(?:厘米|cm|米)',inbound,re.I) else None


def clarification_fingerprint(products,revision,slot,text):
    return hashlib.sha256(json.dumps([products,revision,slot,text],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def required_clarification(question,history):
    """Deterministic collection of a missing quote condition, never a quote."""
    if re.search('退款|退订|退费|赔偿|发票|优惠券|优惠劵|投诉',question):return None
    candidates=[]
    if re.search('多人|团购|一起出游',question) and '优惠' in question:candidates.append('一共几位出游呀？')
    if re.search('多少钱|报价|价格|费用.{0,8}(怎么算|多少)|收费.{0,8}(怎么算|多少)',question):
        candidates.extend(['请问计划哪天出游呀？','请问小朋友多大了？'])
    return next((answer for answer in candidates if clarification_slot(question,history,answer)),None)
