"""Calls carried by a node's outbound poll; no retries or resource ownership."""
import secrets
import threading
import time


class NodeLink:
    def __init__(self):
        self._condition = threading.Condition()
        self._calls = {}
        self._pending = {}
        self._closed = False

    def poll(self, node_id, wait=25):
        with self._condition:
            pending = self._pending.setdefault(node_id, {})
            self._condition.wait_for(lambda: self._closed or pending, wait)
            commands = [{'id': call_id, **call['request'], 'body_size': len(call['body'])}
                        for call_id, call in pending.items()]
            pending.clear()
            return commands

    def call(self, node_id, request, body=b'', deadline=None):
        call_id = secrets.token_hex(16)
        call = {'node_id': node_id, 'request': request, 'body': body, 'response': None}
        with self._condition:
            self._calls[call_id] = call
            self._pending.setdefault(node_id, {})[call_id] = call
            self._condition.notify_all()
            try:
                wait = None if deadline is None else max(0, deadline - time.monotonic())
                self._condition.wait_for(lambda: self._closed or call['response'] is not None, wait)
                if call['response'] is None:
                    raise ConnectionError('node call result is unknown; query the original operation')
                return call['response']
            finally:
                self._calls.pop(call_id, None)
                self._pending[node_id].pop(call_id, None)

    def body(self, node_id, call_id):
        with self._condition:
            call = self._calls[call_id]
            if call['node_id'] != node_id:
                raise KeyError(call_id)
            return call['body']

    def reply(self, node_id, call_id, status, headers, body):
        with self._condition:
            call = self._calls[call_id]
            if call['node_id'] != node_id:
                raise KeyError(call_id)
            call['response'] = {'status': status, 'headers': headers, 'body': body}
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
