"""A Mac with an HTTP system proxy must be able to reach its HTTPS center."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_connector import exchange


class NodeConnectorProxyTests(unittest.TestCase):
    def test_https_uses_connect_proxy_and_honors_bypass(self):
        for bypass in (False, True):
            with self.subTest(bypass=bypass), \
                    patch('urllib.request.getproxies', return_value={
                        'https': 'http://worker:p%40ss@127.0.0.1:7890'}), \
                    patch('urllib.request.proxy_bypass', return_value=bypass), \
                    patch('node_connector.http.client.HTTPSConnection') as connection:
                client = connection.return_value
                response = client.getresponse.return_value
                response.status, response.length = 200, 2
                response.getheaders.return_value = []
                response.read.return_value = b'{}'
                self.assertEqual(exchange('https://center.example:8443/base', 'POST', '/node-connect/poll',
                                          b'{}')['body'], b'{}')
                if bypass:
                    connection.assert_called_once_with('center.example', 8443, timeout=35)
                    client.set_tunnel.assert_not_called()
                else:
                    connection.assert_called_once_with('127.0.0.1', 7890, timeout=35)
                    client.set_tunnel.assert_called_once_with('center.example', 8443,
                        headers={'Proxy-Authorization': 'Basic d29ya2VyOnBAc3M='})
                client.request.assert_called_once_with('POST', '/base/node-connect/poll', body=b'{}',
                    headers={'User-Agent': 'phonefarm-target/1'})
                client.close.assert_called_once()
