"""Business intake and job sources; execution owns the only resource queue."""
import os
import re
import secrets
import json
import time
import uuid
import threading

from job_targets import JobTargets
from job_artifacts import JobArtifacts
from job_dispatch import request_fingerprint
from job_queue import RequestConflict
from node_directory import DirectoryError, DEADLINE_S
import job_selection


class JobCenter:
    def __init__(self, local):
        self.local = local
        self.artifacts = JobArtifacts(local)
        self.targets = JobTargets(local.dispatch.queue)
        self._guard = threading.Lock()
        self._stopping = {}
        self._closed = False

    def prepare_request(self, spec):
        """Resolve business references once, before selecting an execution source."""
        if not isinstance(spec, dict):
            raise ValueError('job spec must be an object')
        spec = dict(spec)
        actor_id = spec.get('actor_id')
        if actor_id is not None:
            if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 64:
                raise ValueError('actor_id 必须是 1-64 字符的非空字符串')
            spec['actor_id'] = actor_id.strip()
        research_id = spec.get('research_id')
        research_devices = []
        store = getattr(self.local, 'research', None)
        if research_id is not None:
            try:
                if store is None:
                    raise ValueError('this node keeps no research records')
                research_devices = store.get('research', research_id)['data'].get('device_requirements') or []
            except ValueError as error:
                raise ValueError(f'research_id must name a research on this node: {error}') from None
        direction_id = spec.get('direction_id')
        if direction_id is not None:
            if research_id is None:
                raise ValueError('direction_id requires research_id')
            try:
                if store is None:
                    raise ValueError('this node keeps no research records')
                if store.get('direction', direction_id)['research_id'] != research_id:
                    raise ValueError('direction_id must belong to research_id')
            except ValueError as error:
                raise ValueError(f'direction_id must name a direction on research {research_id}: {error}') from None
        return {'spec': spec, 'research_devices': list(research_devices)}

    def select_request(self, spec):
        return job_selection.select(self.local, self.prepare_request(spec))

    def start(self, spec, body, request_id=None, *, admit=None, selection=None):
        key = self.local.dispatch.request_key(request_id or uuid.uuid4().hex)
        previous = self.targets.by_request(key)
        fingerprint = request_fingerprint(spec, body)
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise RequestConflict('request ID was already used with different content')
            if previous.get('stop') or self.refresh(previous):
                return self.status(previous['id'])
            return self.transfer(previous, body)
        node_id, request, phone = selection or self.select_request(spec)
        if node_id == self.local.node_id and phone is None:
            return self.local.accept(request, body, key, job_id=secrets.token_hex(6), admit=admit)
        # The center's queue never reserves the worker's CPU or phone.
        if self.local.dispatch.queue.by_request(key):
            raise RequestConflict('request ID already belongs to a local job')
        if node_id != self.local.node_id:
            self.node(node_id)
        record = self.targets.reserve({'id': secrets.token_hex(6), 'request_id': key, 'fingerprint': fingerprint,
            'execution_node_id': node_id, 'request': request, 'submitted_at': time.time(),
            'device_selection': ({key: phone[key] for key in ('ref', 'node_id', 'device_id', 'serial', 'model')}
                                 if phone else None),
            'sent_at': None, 'stop': None, 'observation': None})
        return self.transfer(record, body)

    def node(self, node_id):
        entry = next((entry for entry in self.local.directory.list() if entry['node_id'] == node_id), None)
        if entry is None:
            raise ValueError('execution node is not registered: ' + str(node_id))
        return entry

    def remote(self, jid):
        record = self.targets.get(jid)
        return record if record and record['execution_node_id'] != self.local.node_id else None

    def rpc(self, record, method, path, body=b'', headers=None, *, max_bytes=2 << 20, deliver=None):
        return self.local.directory.link.call(record['execution_node_id'], {
            'method': method, 'path': path, 'headers': headers or {}, 'max_bytes': max_bytes}, body,
            deadline=time.monotonic() + DEADLINE_S, deliver=deliver)

    def observe(self, record, observation):
        finished = (observation.get('queue', {}).get('state') == 'done'
                    and (record.get('observation') or {}).get('queue', {}).get('state') != 'done')
        record = self.targets.update(record['id'], observation=observation, observed_at=time.time())
        if record['execution_node_id'] != self.local.node_id:
            self.artifacts.observed(record)
            parent = record['request']['spec'].get('recovery_for') if finished else None
            while parent:
                original = self.remote(parent)
                self.artifacts.recollect(parent)
                parent = original['request']['spec'].get('recovery_for')
        if record['stop'] and observation.get('queue', {}).get('state') != 'done':
            self._send_stop_in_background(record)

    def refresh(self, record):
        if record['execution_node_id'] == self.local.node_id:
            self.observe(record, self.local.status(record['id']))
            return True
        if ((record.get('observation') or {}).get('queue', {}).get('state') == 'done'
                or self.node(record['execution_node_id'])['connection']['status'] == 'disconnected'):
            return record.get('observation') is not None
        try:
            response = self.rpc(record, 'GET', '/jobs/' + record['id'])
        except ConnectionError:
            return False
        if response['status'] == 200:
            self.observe(record, json.loads(response['body']))
            return True
        return False

    def transfer(self, record, body):
        if record.get('stop'):
            return self.status(record['id'])
        if record['execution_node_id'] == self.local.node_id:
            from job_capacity import check_fit
            if self.targets.mark_sent(record['id']):
                job = self.local.accept(record['request'], body, record['request_id'], job_id=record['id'],
                    admit=lambda: check_fit(record['request']['spec'], self.local.dispatch.queue.capacity()))
                self.observe(record, job)
            return self.status(record['id'])
        try:
            response = self.rpc(record, 'POST', '/node/executions/' + record['id'], body,
                {'X-Execution': json.dumps({**record['request'], 'center_id': self.local.node_id}),
                 'X-Request-ID': 'center:' + self.local.node_id + ':' + record['id']},
                deliver=lambda: self.targets.mark_sent(record['id']))
        except ConnectionError:
            return self.status(record['id'])
        value = json.loads(response['body'])
        if response['status'] != 200:
            raise DirectoryError(response['status'], value.get('error', 'execution request failed'))
        self.observe(record, value)
        return self.status(record['id'])

    def status(self, jid):
        record = self.targets.get(jid)
        if record['execution_node_id'] == self.local.node_id and record['observation'] is not None:
            return {**self.local.status(jid), 'device_selection': record['device_selection']}
        if record['observation'] is not None:
            value = dict(record['observation'])
        else:
            spec = record['request']['spec']
            value = {**spec, 'id': jid, 'started': record['submitted_at'], 'state': 'unknown',
                     'phase': 'submission_unknown', 'exit': None,
                     'queue': {'state': 'unknown', 'resources': []},
                     'recovery': {'status': 'unknown', 'reason': 'execution has not been reported'}}
            if record['stop'] and record['sent_at'] is None:
                value.update(state='finished', phase='finished', outcome='cancelled', ended=record['stop']['at'],
                             queue={'state': 'done', 'resources': []},
                             recovery={'status': 'not_needed', 'reason': 'cancelled before delivery'})
        for key in ('actor_id', 'research_id', 'direction_id'):
            if key in record['request']['spec']:
                value.setdefault(key, record['request']['spec'][key])
        value.update(request_id=record['request_id'], execution_node_id=record['execution_node_id'],
                     observed_at=record.get('observed_at'),
                     stop_requested=record['stop'],
                     device_selection=record['device_selection'],
                     artifacts_sync=record.get('artifacts_sync', {'status': 'pending'}),
                     connection=self.node(record['execution_node_id'])['connection'])
        return value

    def operation(self, jid, method, suffix, body=None, request_id=None):
        record = self.remote(jid)
        try:
            response = self.rpc(record, method, '/jobs/' + jid + suffix,
                b'' if body is None else json.dumps(body).encode(),
                {'Content-Type': 'application/json', **({'X-Request-ID': request_id} if request_id else {})})
        except ConnectionError as error:
            raise DirectoryError(503, str(error)) from error
        value = json.loads(response['body'])
        if response['status'] != 200:
            from node_session import SessionError
            raise SessionError(response['status'], value.get('code', 'execution_unavailable'),
                               value.get('error', 'execution operation failed'))
        if 'node_id' in value:
            value.update(node_id=self.local.node_id, execution_node_id=record['execution_node_id'])
        return value

    def recover(self, jid, kind, body, request_id):
        if body != {}:
            raise ValueError('recovery body must be empty; the original node derives the session')
        parent = self.remote(jid)
        key = self.local.dispatch.request_key(request_id)
        recovery = {'source_job_id': jid, 'kind': kind, 'body': body}
        original = parent['request']['spec']
        spec = {'bin': 'phonefarm', 'argv': [], 'device_access': 'recovery', 'recovery_for': jid,
                **{key: original[key] for key in ('actor_id', 'research_id', 'direction_id') if key in original}}
        record = self.targets.reserve({'id': secrets.token_hex(6), 'request_id': key,
            'fingerprint': request_fingerprint(recovery, b''), 'execution_node_id': parent['execution_node_id'],
            'request': {'spec': spec, 'research_devices': [], 'recovery': recovery},
            'device_selection': parent.get('device_selection'), 'submitted_at': time.time(),
            'sent_at': None, 'stop': None, 'observation': None})
        if record.get('stop') or record.get('observation') or record.get('sent_at') and self.refresh(record):
            return self.status(record['id'])
        return self.transfer(record, b'')

    def reset(self, jid, note):
        self.stop(jid, 'reset', {'note': note})
        status = self.status(jid)
        released = status['queue']['state'] == 'done'
        return {'released': released,
                'storage_committed': released and status['state'] == 'finished' and 'storage_error' not in status,
                'pending': not released, 'job': status}

    def cancel(self, jid, force):
        self.stop(jid, 'cancel', {'force': force})
        status = self.status(jid)
        return {'accepted': True, 'pending': status['queue']['state'] != 'done', 'job': status}

    def stop(self, jid, action, body):
        record = self.targets.update(jid, stop={'at': time.time(), 'action': action, **body})
        if (record['observation'] is not None
                and self.node(record['execution_node_id'])['connection']['status'] != 'disconnected'):
            thread = self._send_stop_in_background(record, explicit=True)
            if thread is not None:
                thread.join()

    def deliver_stop(self, record, explicit=False):
        if not explicit and (record['observation'] or {}).get('queue', {}).get('state') == 'done':
            return
        try:
            response = self.rpc(record, 'POST', '/jobs/' + record['id'] + '/' + record['stop']['action'],
                json.dumps({key: value for key, value in record['stop'].items() if key not in ('at', 'action')}).encode(),
                {'Content-Type': 'application/json'})
        except ConnectionError:
            return
        if response['status'] == 200:
            self.observe(record, json.loads(response['body'])['job'])

    def report(self, node_id, observations, events=()):
        self.targets.import_events(node_id, events)
        for observation in observations:
            record = self.targets.get(observation['id'])
            if record is None or record['execution_node_id'] != node_id:
                continue  # A machine reports only its own already-routed public jobs.
            self.observe(record, observation)
        for record in self.targets.rows():
            if record['execution_node_id'] == node_id:
                self.artifacts.observed(record)
            if (record['execution_node_id'] == node_id and record['stop'] and record['observation'] is not None
                    and record['observation'].get('queue', {}).get('state') != 'done'):
                self._send_stop_in_background(record)

    def _send_stop_in_background(self, record, explicit=False):
        def run():
            try:
                self.deliver_stop(record, explicit)
            finally:
                with self._guard:
                    self._stopping.pop(record['id'])
        with self._guard:
            if self._closed:
                return
            if record['id'] in self._stopping:
                return self._stopping[record['id']]
            thread = threading.Thread(target=run, daemon=True, name='center-reset')
            self._stopping[record['id']] = thread
            thread.start()
            return thread

    def close(self):
        with self._guard:
            self._closed = True
            threads = list(self._stopping.values())
        for thread in threads:
            thread.join()
        self.artifacts.close()

    def by_request(self, key):
        record = self.targets.by_request(key)
        if record:
            self.refresh(record)
            return self.status(record['id'])
        row = self.local.dispatch.queue.by_request(key)
        if row:
            return self.local.status(row['job_id'])
        raise KeyError(key)

    def job_ids(self):
        # The local source is already durable. Do not copy its queue into a second ledger.
        return {jid for jid in os.listdir(self.local.dir) if re.fullmatch(r'[0-9a-f]{12}', jid)} | {
            row['job_id'] for row in self.local.dispatch.queue.rows()} | {
            row['id'] for row in self.targets.rows()}

    def task_rows(self):
        rows = self.local.dispatch.queue.rows()
        for record in self.targets.rows():
            if record['execution_node_id'] == self.local.node_id:
                continue
            status = record.get('observation') or {'queue': {'state': (
                'done' if record.get('stop') and record.get('sent_at') is None else 'unknown')}}
            rows.append({'job_id': record['id'], 'spec': record['request']['spec'],
                         'state': status['queue']['state'], 'status_committed_at': status.get('status_committed_at')})
        return rows

    def queue_snapshot(self):
        local = self.local.dispatch.snapshot()
        nodes = [{'node_id': self.local.node_id, 'name': 'local', 'observed_at': time.time(),
                  'connection': {'status': 'ready'}, 'queue': local}]
        entries = self.local.directory.list() if self.local.directory else []
        nodes.extend({'node_id': entry['node_id'], 'name': entry['name'], 'connection': entry['connection'],
                      'observed_at': (entry.get('snapshot') or {}).get('at'),
                      'queue': (entry.get('snapshot') or {}).get('queue')} for entry in entries)
        return {**local, 'node_id': self.local.node_id, 'execution_queues': nodes}
