import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'scripts/import-tongcheng-knowledge.py'
spec=importlib.util.spec_from_file_location('tc_import',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ImportTests(unittest.TestCase):
    def test_readonly_idempotent_and_never_self_approves(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.sqlite';db=sqlite3.connect(source)
            for table in ('rag_documents','shared_rag_documents'):
                db.execute('CREATE TABLE '+table+'(id INTEGER,title TEXT,content TEXT,item_id TEXT,enabled INTEGER,valid_until TEXT)')
                db.execute('INSERT INTO '+table+' VALUES(1,?,?,?,?,?)',('已存在资料','原文内容','other-platform-product',1,'2027-01-01'))
            db.commit();db.close()
            before=source.read_bytes();first=module.collect(source);second=module.collect(source)
            self.assertEqual(first,second);self.assertEqual(before,source.read_bytes())
            self.assertEqual(len(first),2);self.assertNotEqual(first[0]['id'],first[1]['id'])
            for item in first:
                self.assertFalse(item['reviewed']);self.assertFalse(item['enabled_for_tongcheng'])
                self.assertEqual(item['original_item_id'],'other-platform-product')
                self.assertEqual(item['original_valid_until'],'2027-01-01')
