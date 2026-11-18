import unittest
from tests.test_tongcheng import Transport
from tongcheng_network import allowed


class NetworkTests(unittest.TestCase):
    def test_lan_requires_enabled_allowlist_and_known_private_host(self):
        config={'lan_enabled':True,'allowed_hosts':['192.168.0.56'],'allowed_networks':['192.168.0.0/24']}
        self.assertTrue(allowed('192.168.0.80','192.168.0.56',config))
        self.assertTrue(allowed('127.0.0.1','192.168.0.56',config))
        self.assertFalse(allowed('192.168.0.80','192.168.0.56',{}))
        self.assertFalse(allowed('192.168.1.80','192.168.0.56',config))
        self.assertFalse(allowed('8.8.8.8','192.168.0.56',config))
        self.assertFalse(allowed('192.168.0.80','attacker.example',config))
        self.assertFalse(allowed('192.168.0.80','localhost',config))
        self.assertTrue(allowed('127.0.0.1','localhost',{}))
