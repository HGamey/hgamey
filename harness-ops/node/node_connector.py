"""Outbound node enrollment and calls; the center never dials the worker."""
import argparse
import http.client
import json
import logging
import os
import socket
import threading
from urllib.parse import urlsplit

from artifact_io import SafeRoot
from node_directory import REFRESH_S
from node_identity import load_identity


def exchange(url, method, path, body=b'', headers=None, *, timeout=35, connections=None, guard=None,
             stop=None, max_bytes=2 << 20):
    endpoint = urlsplit(url)
    cls = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
    connection = cls(endpoint.hostname, endpoint.port, timeout=35)
    sock = None
    try:
        connection.connect()
        sock = connection.sock
        sock.settimeout(timeout)
        if connections is not None:
            with guard:
                if stop.is_set():
                    raise ConnectionError('node connector is closed')
                connections.add(sock)
        connection.request(method, endpoint.path.rstrip('/') + path, body=body, headers=headers or {})
        response = connection.getresponse()
        return {'status': response.status, 'headers': dict(response.getheaders()), 'body': response.read(max_bytes + 1)}
    finally:
        connection.close()
        if connections is not None:
            with guard:
                connections.discard(sock)


def close_sockets(connections, guard):
    with guard:
        sockets = list(connections)
    for sock in sockets:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # A completed request may already have closed its socket.
        sock.close()


def join(center, invite, name, data_root):
    node_id = load_identity(data_root)
    response = exchange(center, 'POST', '/node-connect/join', json.dumps({
        'invite': invite, 'name': name, 'node_id': node_id}).encode(), {'Content-Type': 'application/json'})
    result = json.loads(response['body'])
    if response['status'] != 201:
        raise ValueError(result.get('error', 'node join failed'))
    store = SafeRoot(data_root)
    try:
        store.write_json('_node/center.json', {'url': center, 'center_id': result['center_id'],
                                             'token': result['token']})
    finally:
        store.close()
    return result['node']


class LocalHTTP:
    def __init__(self, url, token, node_id):
        self.url, self.token, self.node_id = url, token, node_id
        self._connections, self._guard, self._stop = set(), threading.Lock(), threading.Event()

    def request(self, method, path, body, headers, max_bytes=2 << 20):
        return exchange(self.url, method, path, body, {**headers,
            'Authorization': 'Bearer ' + self.token, 'X-Expected-Node-ID': self.node_id},
            timeout=None, max_bytes=max_bytes, connections=self._connections, guard=self._guard, stop=self._stop)

    def snapshot(self):
        response = self.request('GET', '/node/snapshot', b'', {})
        if response['status'] != 200:
            raise OSError('local snapshot returned HTTP ' + str(response['status']))
        return json.loads(response['body'])

    def close(self):
        self._stop.set()
        close_sockets(self._connections, self._guard)


class NodeConnector:
    def __init__(self, config, backend):
        self.config, self.backend = config, backend
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._connections = set()
        self._threads = []

    def _send(self, method, path, body=b'', headers=None, max_bytes=2 << 20):
        return exchange(self.config['url'], method, path, body, {
            'Authorization': 'Bearer ' + self.config['token'],
            'X-Expected-Node-ID': self.config['center_id'], **(headers or {})},
            connections=self._connections, guard=self._guard, stop=self._stop, max_bytes=max_bytes)

    def _json(self, path, value):
        response = self._send('POST', path, json.dumps(value).encode(), {'Content-Type': 'application/json'})
        if response['status'] != 200:
            raise OSError('center returned HTTP ' + str(response['status']))
        return json.loads(response['body'])

    def _spawn(self, function, *args):
        def run():
            try:
                function(*args)
            finally:
                with self._guard:
                    self._threads.remove(threading.current_thread())
        with self._guard:
            if self._stop.is_set():
                return
            thread = threading.Thread(target=run, daemon=True, name='node-connector')
            self._threads.append(thread)
            thread.start()

    def start(self):
        self._spawn(self._poll)
        self._spawn(self._report)

    def _poll(self):
        while not self._stop.is_set():
            try:
                for command in self._json('/node-connect/poll', {})['commands']:
                    self._spawn(self._execute, command)
            except (OSError, ValueError, http.client.HTTPException) as error:
                if not self._stop.is_set():
                    logging.warning('node connection interrupted: %s', error)
                    self._stop.wait(1)

    def _report(self):
        while not self._stop.is_set():
            try:
                self._json('/node-connect/report', self.backend.snapshot())
            except (OSError, ValueError, http.client.HTTPException) as error:
                if not self._stop.is_set():
                    logging.warning('node observation upload failed: %s', error)
            self._stop.wait(REFRESH_S)

    def _execute(self, command):
        try:
            path = '/node-connect/calls/' + command['id']
            body = b''
            if command['body_size']:
                response = self._send('GET', path + '/body', max_bytes=command['body_size'])
                if response['status'] != 200:
                    return  # The caller left before execution started.
                body = response['body']
            response = self.backend.request(command['method'], command['path'], body, command['headers'],
                                            max_bytes=command['max_bytes'])
            self._send('POST', path + '/reply', response['body'], {'X-Node-Response': json.dumps({
                'status': response['status'], 'headers': response['headers']})})
        except (OSError, ValueError, http.client.HTTPException) as error:
            if not self._stop.is_set():
                logging.warning('node call response unknown; query its original operation: %s', error)

    def close(self):
        self._stop.set()
        with self._guard:
            threads = list(self._threads)
        close_sockets(self._connections, self._guard)
        self.backend.close()
        for thread in threads:
            thread.join()


def main():
    parser = argparse.ArgumentParser(description='Join this computer to one HGamey center.')
    parser.add_argument('--center', required=True)
    parser.add_argument('--invite', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    node = join(args.center, args.invite, args.name, os.path.abspath(args.data_dir))
    print(json.dumps({'node': node, 'next': 'restart the local node service to connect'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
