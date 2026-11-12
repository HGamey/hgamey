"""A request whose body the server does not read (refused before reading it) must not leave that body on the
connection: Cloudflare carries many people's requests over a few connections to the center, and the leftover bytes
would be read as the start of someone else's next request ("501 Unsupported method ('{}GET')"). Such a response
closes the connection and says so; reads keep theirs."""
import http.client
import json
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

REFUSED = [("POST", "/mcp"), ("POST", "/research-state/x"), ("POST", "/nodes/x"), ("POST", "/node/executions/x"),
           ("POST", "/jobs"), ("POST", "/dash/api/agents/x"), ("POST", "/node/connection/heartbeat"),
           ("POST", "/node-connection/poll"), ("POST", "/uploads/x"), ("POST", "/results"), ("POST", "/games/x"),
           ("POST", "/dash/api/research-state/research/x"), ("PUT", "/uploads/x"), ("DELETE", "/jobs/x"),
           ("POST", "/no/such/route")]


class UnreadBodyTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    write = staticmethod(core.CoreFlowTests.write)

    def test_a_refused_request_body_does_not_become_the_next_request(self):
        for method, path in REFUSED:
            for body in (b"{}", b"x" * 70000):
                with self.subTest(method=method, path=path, size=len(body)):
                    connection = http.client.HTTPConnection(*self.address, timeout=10)
                    try:
                        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
                        refused = connection.getresponse()
                        refused.read()
                        self.assertGreaterEqual(refused.status, 400)
                        # The same connection object: http.client reopens it when the server said it closes.
                        connection.request("GET", "/health")
                        after = connection.getresponse()
                        self.assertEqual((after.status, after.read()), (200, b"ok"))
                    finally:
                        connection.close()

    def test_a_connection_that_ends_says_so_and_reads_keep_theirs(self):
        # Writes end their connection after the response (a route may refuse before reading); the response says so,
        # so the next request opens a new one instead of meeting a closed one.
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        try:
            body = json.dumps(self.write("create-sr-keep", {"title": "长连接", "objective": "保持"})).encode()
            connection.request("POST", "/research-state/research/sr-keep", body=body, headers={
                "Authorization": f"Bearer {core.ADMIN}", "X-Expected-Node-ID": self.node_id, "Content-Type": "application/json"})
            written = connection.getresponse()
            written.read()
            self.assertEqual((written.status, written.getheader("Connection")), (201, "close"))
            connection.request("GET", "/health")
            after = connection.getresponse()
            self.assertEqual((after.status, after.read(), after.will_close), (200, b"ok", False))
            socket = connection.sock
            connection.request("GET", "/health")
            again = connection.getresponse()
            self.assertEqual((again.status, again.read()), (200, b"ok"))
            self.assertIs(connection.sock, socket, "reads keep their connection")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
