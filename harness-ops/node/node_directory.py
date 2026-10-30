"""Joined execution nodes and their outbound observations; no remote URL probes."""
from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time

from artifact_io import SafeRoot
from job_dispatch import file_lock
from node_link import NodeLink

SCHEMA = 'phonefarm/node-directory@2'
REFRESH_S = 30
DEADLINE_S = 8
MAX_RESPONSE = 2 << 20


class DirectoryError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _ProbeError(Exception):
    def __init__(self, status, message, reachable=True):
        self.status, self.message, self.reachable = status, message, reachable


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', value) or value == 'local':
        raise DirectoryError(400, 'name must be 1-64 lowercase letters, digits, underscores or hyphens; local is reserved')
    return value


def _metadata(body):
    if not isinstance(body, dict):
        raise DirectoryError(400, 'node metadata must be a JSON object')
    out = {key: body[key] for key in ('note', 'tags') if key in body}
    if 'note' in out and (not isinstance(out['note'], str) or len(out['note']) > 4096 or '\x00' in out['note']):
        raise DirectoryError(400, 'note must be a string of at most 4096 characters')
    if 'tags' in out:
        if (not isinstance(out['tags'], list) or len(out['tags']) > 64 or any(
                not isinstance(tag, str) or not tag.strip() or len(tag) > 128
                or any(ord(c) < 32 for c in tag) for tag in out['tags'])):
            raise DirectoryError(400, 'tags must be a list of nonempty short strings')
        out['tags'] = list(dict.fromkeys(out['tags']))
    return out


class NodeDirectory:
    def __init__(self, data_root, local_node_id):
        self.local_node_id = local_node_id
        self._guard = threading.RLock()
        self._closed = False
        self.link = NodeLink()
        root = SafeRoot(data_root)
        try:
            root.mkdir('_node')
            root.mkdir('_node/directory')
        finally:
            root.close()
        directory = os.path.join(os.path.realpath(data_root), '_node', 'directory')
        self._store = SafeRoot(directory)
        self._lock_path = os.path.join(directory, 'registry.lock')
        with file_lock(self._lock_path):
            self._store.publish_json_once('registry.json', {'schema': SCHEMA, 'nodes': [], 'invites': []})
            state = self._read()
            if state['schema'] == 'phonefarm/node-directory@1':
                # Keep names, identities and historical observations, discard obsolete dial-in credentials.
                fields = ('name', 'node_id', 'tags', 'note', 'revision', 'created_at', 'updated_at', 'snapshot')
                state = {'schema': SCHEMA, 'invites': [], 'nodes': [
                    {**{key: entry[key] for key in fields}, 'last_seen_at': None}
                    for entry in state['nodes']]}
                self._store.write_json('registry.json', state)

    def _read(self):
        return self._store.read_json('registry.json', maximum=128 << 20)

    @contextmanager
    def _locked(self):
        with self._guard:
            if self._closed:
                raise DirectoryError(503, 'node directory is closed')
            try:
                with file_lock(self._lock_path):
                    yield self._read()
            except DirectoryError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise DirectoryError(500, 'node directory storage is unavailable') from None

    @staticmethod
    def _find(state, name):
        entry = next((entry for entry in state['nodes'] if entry['name'] == name), None)
        if entry is None:
            raise DirectoryError(404, 'registered node was not found')
        return entry

    def _public(self, entry):
        value = copy.deepcopy({key: val for key, val in entry.items() if key != 'credential'})
        last_seen = entry.get('last_seen_at')
        connected = last_seen is not None and time.time() - last_seen <= 2 * REFRESH_S
        value['connection'] = {'status': 'ready' if connected else 'disconnected',
                               'last_seen_at': last_seen}
        value['stale'] = not connected or value.get('snapshot') is None
        return value

    def list(self):
        with self._locked() as state:
            return [self._public(entry) for entry in state['nodes']]

    def get(self, name):
        with self._locked() as state:
            return self._public(self._find(state, name))

    def invite(self, hours=24, note=''):
        _metadata({'note': note})
        try:
            seconds = float(hours) * 3600
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        if not math.isfinite(seconds) or seconds <= 0:
            raise DirectoryError(400, 'hours must be a positive finite number')
        code = 'hgninv_' + secrets.token_urlsafe(32)
        now = time.time()
        entry = {'id': secrets.token_hex(8), 'sha256': hashlib.sha256(code.encode()).hexdigest(),
                 'created_at': now, 'expires_at': now + seconds, 'note': note}
        with self._locked() as state:
            state['invites'].append(entry)
            self._store.write_json('registry.json', state)
        return {**{key: val for key, val in entry.items() if key != 'sha256'}, 'code': code}

    def invites(self):
        with self._locked() as state:
            return [{key: val for key, val in entry.items() if key != 'sha256'}
                    for entry in state['invites'] if 'used_at' not in entry]

    def revoke_invite(self, invite_id):
        with self._locked() as state:
            state['invites'] = [entry for entry in state['invites'] if entry['id'] != invite_id]
            self._store.write_json('registry.json', state)
        return {'id': invite_id, 'revoked': True}

    def join(self, body):
        name = _name(body.get('name'))
        node_id = body.get('node_id')
        if not isinstance(node_id, str) or not re.fullmatch(r'[0-9a-f]{32}', node_id):
            raise DirectoryError(400, 'node_id must be the local persistent 32-character identity')
        if node_id == self.local_node_id:
            raise DirectoryError(409, 'an execution node cannot use the center identity')
        code = body.get('invite')
        if not isinstance(code, str) or not code:
            raise DirectoryError(400, 'invite is required')
        metadata = _metadata(body)
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self._locked() as state:
            invite = next((entry for entry in state['invites']
                           if hmac.compare_digest(entry['sha256'], digest)), None)
            if invite is None:
                raise DirectoryError(404, 'invite not found')
            if 'used_at' in invite:
                raise DirectoryError(409, 'invite already used')
            if time.time() >= invite['expires_at']:
                raise DirectoryError(410, 'invite expired')
            identity = next((entry for entry in state['nodes'] if entry['node_id'] == node_id), None)
            if identity is not None and identity['name'] != name:
                raise DirectoryError(409, 'node identity is already registered under a different name; an administrator must rename it')
            entry = next((entry for entry in state['nodes'] if entry['name'] == name), None)
            if entry is not None and entry['node_id'] not in (None, node_id):
                raise DirectoryError(409, 'node name is already registered')
            now = time.time()
            if entry is None:
                entry = {'name': name, 'node_id': node_id, 'tags': [], 'note': '', 'revision': 1,
                         'created_at': now, 'snapshot': None, 'last_seen_at': None}
                state['nodes'].append(entry)
            token = 'hgnode_' + secrets.token_urlsafe(32)
            entry.update(name=name, node_id=node_id, updated_at=now,
                         credential=hashlib.sha256(token.encode()).hexdigest(), **metadata)
            invite.update(used_at=now, node_id=node_id)
            self._store.write_json('registry.json', state)
            return {'center_id': self.local_node_id, 'node': self._public(entry), 'token': token}

    def verify(self, token):
        if not token.startswith('hgnode_'):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._locked() as state:
            return next((entry['node_id'] for entry in state['nodes']
                         if hmac.compare_digest(entry.get('credential', ''), digest)), None)

    def report(self, node_id, snapshot=None):
        with self._locked() as state:
            entry = next(entry for entry in state['nodes'] if entry['node_id'] == node_id)
            entry['last_seen_at'] = time.time()
            if snapshot is not None:
                entry['snapshot'] = snapshot
            self._store.write_json('registry.json', state)
            return self._public(entry)

    def update(self, name, body):
        fields = _metadata(body)
        if 'name' in body:
            fields['name'] = _name(body['name'])
        with self._locked() as state:
            entry = self._find(state, name)
            if body.get('revision') != entry['revision']:
                raise DirectoryError(409, 'node configuration revision changed; read it again')
            if 'name' in fields and any(other is not entry and other['name'] == fields['name'] for other in state['nodes']):
                raise DirectoryError(409, 'node name is already registered')
            entry.update(fields)
            entry['revision'] += 1
            entry['updated_at'] = time.time()
            self._store.write_json('registry.json', state)
            return self._public(entry)

    def request(self, entry, path, deadline=None, *, headers=None, max_bytes=MAX_RESPONSE):
        try:
            return self.link.call(entry['node_id'], {'method': 'GET', 'path': path, 'headers': headers or {},
                                                   'max_bytes': max_bytes}, deadline=deadline)
        except ConnectionError as error:
            raise _ProbeError('unavailable', str(error)) from error

    def _request(self, entry, path, deadline):
        response = self.request(entry, path, deadline)
        if response['status'] != 200:
            raise _ProbeError('unavailable', 'node returned an unsuccessful HTTP status')
        if len(response['body']) > MAX_RESPONSE:
            raise _ProbeError('incompatible', 'node response exceeds its read limit')
        try:
            value = json.loads(response['body'])
        except (ValueError, UnicodeError):
            raise _ProbeError('incompatible', 'node did not return valid JSON') from None
        return value

    def refresh(self, name=None, devices=False):
        entries = [self.get(name)] if name is not None else self.list()
        observations = []
        for entry in entries:
            path = '/node/snapshot' + ('?devices=1' if devices else '')
            try:
                snapshot = self._request(entry, path, time.monotonic() + DEADLINE_S)
                entry = self.report(entry['node_id'], snapshot)
            except _ProbeError as error:
                entry['connection']['error'] = error.message
            observations.append(entry)
        return observations[0] if name is not None else observations

    def close(self):
        with self._guard:
            self._closed = True
            self.link.close()
            self._store.close()
