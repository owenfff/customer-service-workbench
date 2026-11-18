import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from tongcheng_browser import resolve_anchor_reference


class IdentityTests(unittest.TestCase):
    def open(self,path):
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE IF NOT EXISTS anchors(message TEXT PRIMARY KEY,conversation TEXT NOT NULL)')
        return db

    def test_same_label_is_never_used_to_merge_customers(self):
        with self.open(':memory:') as db:
            a=resolve_anchor_reference(db,['message-a'])
            b=resolve_anchor_reference(db,['message-b'])
            self.assertNotEqual(a,b)
            self.assertEqual(resolve_anchor_reference(db,['message-a','message-new']),a)

    def test_persisted_anchors_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'refs.sqlite'
            db=self.open(path);ref=resolve_anchor_reference(db,['a','b']);db.close()
            db=self.open(path)
            self.assertEqual(resolve_anchor_reference(db,['b','c']),ref);db.close()

    def test_mixed_customer_messages_block_without_learning(self):
        with self.open(':memory:') as db:
            resolve_anchor_reference(db,['a']);resolve_anchor_reference(db,['b'])
            with self.assertRaises(ValueError):resolve_anchor_reference(db,['a','b','c'])
            self.assertEqual(db.execute('SELECT count(*) FROM anchors WHERE message=?',('c',)).fetchone()[0],0)

    def test_empty_and_duplicate_anchors_rejected(self):
        with self.open(':memory:') as db:
            for ids in ([],['a','a']):
                with self.assertRaises(ValueError):resolve_anchor_reference(db,ids)
