"""Attach persisted wire timestamps to the customer conversation view."""
from datetime import datetime, timezone


def timestamp(value):
    if not isinstance(value,str) or not value:return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:return None
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:return None


def conversation_messages(service,transport,conversation):
    messages=[dict(r) for r in service.db.execute('SELECT * FROM messages WHERE account=? AND conversation=? ORDER BY rowid',('local',conversation))]
    roles={}
    if service.db.execute("SELECT 1 FROM sqlite_master WHERE name='message_origins'").fetchone():
        roles={r['id']:r['origin'] for r in service.db.execute('SELECT id,origin FROM message_origins WHERE account=? AND conversation=?',('local',conversation))}
    times={}
    if getattr(transport,'config',{}).get('transport')=='independent_websocket' and conversation.startswith('ws:'):
        times={r['id']:dict(r) for r in transport.db.execute('SELECT i.id,i.at,o.received_at FROM inbox i LEFT JOIN observations o ON i.customer=o.customer AND i.id=o.id WHERE i.customer=?',(conversation[3:],))}
    for message in messages:
        record=times.get(message['id'],{})
        occurred=timestamp(record.get('at'));received=timestamp(record.get('received_at'))
        message.update(outbound_origin=roles.get(message['id']),occurred_at=occurred,received_at=received,display_time=occurred or received,
                       time_source=('sent_record' if message['direction']=='outbound' else 'message_record') if occurred else 'received' if received else 'unknown')
    return messages
