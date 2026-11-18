import sqlite3
import unittest
from types import SimpleNamespace
from tests.test_tongcheng import Transport  # Establish the server import path.
from tongcheng_message_times import conversation_messages


class MessageTimeTests(unittest.TestCase):
    def test_customer_receive_and_agent_send_times_are_persisted_and_scoped(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        wire=sqlite3.connect(':memory:');wire.row_factory=sqlite3.Row
        self.addCleanup(db.close);self.addCleanup(wire.close)
        db.execute('CREATE TABLE messages(account,conversation,id,direction,text,kind)')
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)',[
            ('local','ws:one','customer','inbound','question','text'),
            ('local','ws:one','agent','outbound','reply','text'),
            ('local','ws:one','old','inbound','unknown time','text')])
        wire.execute('CREATE TABLE inbox(customer,id,at)');wire.execute('CREATE TABLE observations(customer,id,received_at)')
        wire.executemany('INSERT INTO inbox VALUES(?,?,?)',[('one','customer',None),('one','agent','2026-10-28T16:00:05+08:00'),('two','customer','2020-01-01T00:00:00Z')])
        wire.execute('INSERT INTO observations VALUES(?,?,?)',('one','customer','2026-10-28T08:00:00Z'))
        rows=conversation_messages(SimpleNamespace(db=db),SimpleNamespace(config={'transport':'independent_websocket'},db=wire),'ws:one')
        self.assertEqual(rows[0]['display_time'],'2026-10-28T08:00:00+00:00')
        self.assertEqual(rows[0]['time_source'],'received')
        self.assertEqual(rows[1]['display_time'],'2026-10-28T08:00:05+00:00')
        self.assertEqual(rows[1]['time_source'],'sent_record')
        self.assertIsNone(rows[2]['display_time'])
