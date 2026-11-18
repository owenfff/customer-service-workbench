import sys
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import tongcheng_app


class LocalAuthenticationTests(unittest.TestCase):
    def test_live_launch_authority_version_and_shadow_revocation(self):
        from unittest.mock import patch,AsyncMock
        from tests.test_shadow_documents import document
        original=tongcheng_app.PRIVATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongcheng_app.PRIVATE=Path(tmp)
                doc=document();doc['source_sha256']='a'*64
                (Path(tmp)/'kdocs-knowledge.json').write_text(json.dumps({'documents':[doc]}),'utf-8')
                with TestClient(tongcheng_app.app) as client:
                    login=json.loads((Path(tmp)/'operator-login.json').read_text())
                    csrf=client.post('/auth/login',json=login).json()['csrf'];headers={'X-CSRF-Token':csrf}
                    service=tongcheng_app.app.state.service
                    tongcheng_app.app.state.browser.wire_status={'online':True}
                    tongcheng_app.app.state.browser.session={'source_uid':'test-source','userId':'test-user','spaceId':'test-space','tenant_code':'test-tenant'}
                    payload={'authorize_live_sending':True,'knowledge_source_sha256':'a'*64,'user_instruction':'用户明确要求直接实战'}
                    with patch.object(tongcheng_app,'model_is_available',return_value=True),patch.object(tongcheng_app,'start',new=AsyncMock()),patch.object(tongcheng_app,'open_browser',new=AsyncMock()):
                        self.assertEqual(client.post('/production/enable',json=payload).status_code,403)
                        denied=dict(payload,authorize_live_sending=False)
                        self.assertEqual(client.post('/production/enable',json=denied,headers=headers).status_code,409)
                        bad=dict(payload,knowledge_source_sha256='b'*64)
                        self.assertEqual(client.post('/production/enable',json=bad,headers=headers).status_code,409)
                        self.assertFalse((Path(tmp)/'production-intent.json').exists())
                        response=client.post('/production/enable',json=payload,headers=headers)
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.json()['mode'],'auto')
                    self.assertFalse(response.json()['customer_delivery_verified'])
                    self.assertEqual(len(service.approved_documents()),1)
                    tongcheng_app.app.state.browser.session['source_uid']='different-account'
                    self.assertFalse(client.get('/production').json()['authorized'])
                    self.assertEqual(client.put('/policy',json={'mode':'shadow'},headers=headers).status_code,200)
                    self.assertFalse(client.get('/production').json()['authorized'])
                    self.assertEqual(client.post('/production/resume',headers=headers).json()['status'],'not_authorized_or_paused')
        finally:tongcheng_app.PRIVATE=original

    def test_login_csrf_and_private_state(self):
        original=tongcheng_app.PRIVATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tongcheng_app.PRIVATE=Path(tmp)
                with TestClient(tongcheng_app.app) as client:
                    self.assertEqual(client.get('/state').status_code,401)
                    self.assertEqual(client.get('/audit').status_code,401)
                    self.assertEqual(client.get('/readiness').status_code,401)
                    self.assertEqual(client.get('/shadow-tests/latest').status_code,401)
                    self.assertEqual(client.get('/shadow-tests/knowledge').status_code,401)
                    self.assertEqual(client.get('/shadow-tests/customers').status_code,401)
                    self.assertEqual(client.get('/shadow/live/data').status_code,401)
                    credentials=json.loads((Path(tmp)/'operator-login.json').read_text())
                    result=client.post('/auth/login',json=credentials)
                    self.assertEqual(result.status_code,200)
                    csrf=result.json()['csrf']
                    self.assertEqual(client.get('/state').status_code,200)
                    self.assertEqual(client.get('/shadow/live').status_code,200)
                    live=client.get('/shadow/live/data').json()
                    self.assertFalse(live['historical_replay']);self.assertEqual(live['items'],[])
                    self.assertEqual(client.get('/shadow-tests/latest').status_code,404)
                    self.assertEqual(client.get('/knowledge/documents').json()['imported'],False)
                    self.assertEqual(client.get('/shadow-tests/knowledge').status_code,404)
                    self.assertEqual(client.get('/shadow-tests/customers').status_code,404)
                    self.assertEqual(client.post('/pause').status_code,403)
                    self.assertEqual(client.post('/pause',headers={'X-CSRF-Token':csrf}).status_code,200)
                    self.assertEqual(client.put('/policy',json={'mode':'test_auto','conversations':['not_verified']},headers={'X-CSRF-Token':csrf}).status_code,409)
                    self.assertEqual(client.get('/conversations').json(),{'items':[]})
                    ready=client.get('/readiness').json()
                    self.assertFalse(ready['manual_test_ready'])
                    self.assertFalse(ready['draft_prerequisites_ready'])
                    self.assertEqual(ready['real_send_acceptance'],'not_accepted')
                    self.assertEqual(client.get('/knowledge-inbox').json(),{'items':[]})
                    inbox=[{'id':'source-1','title':'原商品资料','content':'审核原文','content_hash':'hash','original_item_id':'xianyu-item'}]
                    (Path(tmp)/'knowledge-inbox.json').write_text(json.dumps(inbox),'utf-8')
                    faq={'id':'faq','question':'产品说明','answer':'审核原文','valid_from':'2026-01-01T00:00:00Z','expires_at':'2027-01-01T00:00:00Z','reviewed':True,'enabled':True,'version':1}
                    headers={'X-CSRF-Token':csrf}
                    tongcheng_app.app.state.service.audit('test_event','test',status='verified')
                    tongcheng_app.app.state.service.db.commit()
                    logged=client.get('/audit?limit=1').json()
                    self.assertEqual(logged['timezone'],'Asia/Shanghai')
                    self.assertEqual(logged['items'][0]['details']['status'],'verified')
                    self.assertIn('time_beijing',logged['items'][0])
                    self.assertFalse(client.get('/notifications/feishu').json()['configured'])
                    hook={'enabled':True,'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-private-hook','secret':'synthetic-private-signing-secret'}
                    self.assertEqual(client.put('/notifications/feishu',json=hook).status_code,403)
                    result=client.put('/notifications/feishu',json=hook,headers=headers)
                    self.assertEqual(result.status_code,200)
                    self.assertNotIn('synthetic-private',result.text)
                    self.assertNotIn('synthetic-private',client.get('/notifications/feishu').text)
                    self.assertEqual(client.put('/notifications/feishu',json={'enabled':False},headers=headers).status_code,200)
                    self.assertEqual(client.post('/confirm-delivery/missing',headers=headers).status_code,409)
                    db=tongcheng_app.app.state.service.db
                    db.execute("INSERT INTO tasks(id,account,conversation,message,text,status,reason) VALUES('sent','local','ws:test','message','reply','server_accepted','protocol_only')");db.commit()
                    self.assertEqual(client.post('/confirm-delivery/sent',headers=headers).status_code,200)
                    self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='sent'").fetchone()[0],'customer_confirmed')
                    self.assertEqual(client.post('/confirm-delivery/sent',headers=headers).status_code,409)
                    self.assertEqual(client.put('/knowledge/faq?source_ref=source-1',json=faq,headers=headers).status_code,409)
                    self.assertEqual(client.put('/knowledge/faq?source_ref=source-1&product_id=tongcheng-item',json=faq,headers=headers).status_code,200)
                    self.assertEqual(client.get('/knowledge').json()['items'][0]['product_ids'],['tongcheng-item'])
                    invalid=client.post('/auth/login',json={'username':{},'password':'private-test-value'})
                    self.assertNotIn('private-test-value',invalid.text)
        finally:tongcheng_app.PRIVATE=original


if __name__=='__main__':unittest.main()
