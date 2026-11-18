"""Feishu custom-bot transport; callers own durable delivery and deduplication."""
import hashlib
import hmac
import base64
import json
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def build_request(webhook, text, *, secret=None, timestamp=None):
    url = urlsplit(webhook)
    if (url.scheme != 'https' or url.hostname != 'open.feishu.cn' or url.port is not None
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith('/open-apis/bot/v2/hook/')
            or not url.path.removeprefix('/open-apis/bot/v2/hook/')):
        raise ValueError('invalid_feishu_webhook')
    if not text or len(text.encode('utf-8')) > 4096:
        raise ValueError('invalid_notification_text')
    body = {'msg_type': 'text', 'content': {'text': text}}
    if secret:
        stamp = str(int(time.time() if timestamp is None else timestamp))
        key = (stamp + '\n' + secret).encode('utf-8')
        body.update(timestamp=stamp, sign=base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode('ascii'))
    return Request(webhook, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                   headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')


def send(webhook, text, *, secret=None, opener=urlopen):
    """Return delivery status only; never expose URL, payload or exception text."""
    try:
        request = build_request(webhook, text, secret=secret)
        with opener(request, timeout=3) as response:
            data = json.loads(response.read(16384))
        return {'ok': data.get('code') == 0, 'reason': 'accepted' if data.get('code') == 0 else 'remote_rejected'}
    except (ValueError, TypeError):
        return {'ok': False, 'reason': 'invalid_configuration_or_response'}
    except Exception:
        return {'ok': False, 'reason': 'transport_failed'}
