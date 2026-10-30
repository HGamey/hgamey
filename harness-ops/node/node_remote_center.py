"""Route center-authenticated remote operations over the existing machine connection."""
import json
from urllib.parse import unquote, quote

from node_remote import RemoteError, INPUT_CONTRACT, UNSUPPORTED


class RemoteCenter:
    def __init__(self, jobs, local):
        self.jobs, self.local = jobs, local

    def target(self, reference):
        if '/' not in reference:
            return self.jobs.node_id, reference
        node_id, serial = reference.split('/', 1)
        if node_id != self.jobs.node_id:
            self.jobs.center.node(node_id)
        return node_id, unquote(serial)

    def call(self, node_id, operation, args):
        if node_id == self.jobs.node_id:
            if self.local is None:
                raise RemoteError(503, 'PF remote owner is not configured on this computer')
            return getattr(self.local, operation)(**args)
        try:
            response = self.jobs.center.rpc({'execution_node_id': node_id}, 'POST', '/node/remote',
                json.dumps({'operation': operation, 'args': args}).encode(),
                {'Content-Type': 'application/json'}, max_bytes=8 << 20)
        except ConnectionError as error:
            raise RemoteError(503, str(error), query_required=operation == 'do') from error
        headers = {key.lower(): value for key, value in response['headers'].items()}
        if response['status'] in (200, 202) and 'x-meta' in headers:
            return json.loads(headers['x-meta']), response['body'], headers['content-type']
        value = json.loads(response['body'])
        if response['status'] not in (200, 202):
            raise RemoteError(response['status'], value.pop('error', 'remote operation failed'), **value)
        return value

    def do(self, op, ip, serial, action, params, body=None):
        node_id, raw = self.target(serial)
        return self.call(node_id, 'do', {'op': op, 'ip': ip, 'serial': raw,
                                      'action': action, 'params': params, 'body': body})

    def query(self, op, serial, **query):
        node_id, raw = self.target(serial)
        return self.call(node_id, 'query', {'op': op, 'serial': raw, **query})

    def resolve(self, serial):
        node_id, raw = self.target(serial)
        return self.call(node_id, 'resolve', {'serial': raw})

    def devices(self):
        local = self.local.devices() if self.local else {'devices': [], 'capabilities': {}}
        sources = [(self.jobs.node_id, 'local', local, None, {'status': 'ready'})]
        for entry in self.jobs.directory.list():
            snapshot = entry.get('snapshot') or {}
            sources.append((entry['node_id'], entry['name'], snapshot.get('remote') or {},
                            snapshot.get('at'), entry['connection']))
        devices, global_holders, caps = [], [], []
        for node_id, name, value, observed_at, connection in sources:
            caps.append(value.get('capabilities') or {})
            for device in value.get('devices', []):
                raw = device['serial']
                devices.append({**device, 'serial': node_id + '/' + quote(raw, safe=''),
                    'raw_serial': raw, 'execution_node_id': node_id, 'node_name': name,
                    'observed_at': observed_at, 'connection': connection})
            global_holders.extend({**holder, 'execution_node_id': node_id} for holder in value.get('global_holders', []))
        return {'devices': devices, 'global_holders': global_holders,
                'lock_storage_error': local.get('lock_storage_error'),
                'capabilities': {'available': any(cap.get('available') for cap in caps),
                    'actions': sorted({action for cap in caps for action in cap.get('actions', [])}),
                    'input_contract': INPUT_CONTRACT, 'ownership': 'strong-lease-v1',
                    'device_binding_v1': any(cap.get('device_binding_v1') for cap in caps),
                    'unsupported': sorted(UNSUPPORTED)}}
