"""Materialize remote originals one file at a time; execution resources are unrelated."""
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlencode

from artifact_io import relative_path
from node_directory import DirectoryError

CHUNK = 1 << 20


class JobArtifacts:
    def __init__(self, executor):
        self.executor = executor
        self.store = executor._read_root
        self._guard = threading.Lock()
        self._files, self._threads = {}, {}
        self._closed = False

    @property
    def center(self):
        return self.executor.center

    def require_job(self, jid):
        self.executor.path(jid)
        if self.center.remote(jid) or self.executor.dispatch.queue.get(jid):
            return
        os.close(self.store.open_fd(jid, directory=True))

    def request(self, jid, path, *, headers=None, maximum=2 << 20):
        record = self.center.remote(jid)
        try:
            response = self.center.rpc(record, 'GET', path, headers=headers, max_bytes=maximum)
        except ConnectionError as error:
            raise DirectoryError(503, str(error)) from error
        if response['status'] == 404:
            raise FileNotFoundError(path)
        if response['status'] not in (200, 206):
            raise DirectoryError(response['status'], 'execution source could not be read: ' + path)
        return response

    def source(self, jid):
        record = self.center.remote(jid)
        saved = record.get('artifact_source')
        if saved and saved['job']['state'] == 'finished' and saved['completion_problem'] is None:
            return saved
        value = json.loads(self.request(jid, '/node/executions/' + jid + '/source')['body'])
        self.center.targets.update(jid, artifact_source=value)
        return value

    def metadata(self, jid):
        if self.center.remote(jid):
            return self.source(jid)['job']
        return self.executor._read_meta(jid)

    def completion_problem(self, jid):
        if self.center.remote(jid):
            if self.center.status(jid)['state'] != 'finished':
                return 'source job has not finished'
            return self.source(jid)['completion_problem']
        return self.executor.source_completion_problem(jid, self.executor.status(jid))

    def _directories(self, jid, path):
        self.store.mkdir(jid)
        self.store.mkdir(jid + '/out')
        self.store.mkdir(jid + '/.collected')
        parent = jid + '/out'
        for part in path.split('/')[:-1]:
            if part:
                parent += '/' + part
                self.store.mkdir(parent)

    def _receipt(self, path):
        try:
            return self.store.read_json(path)
        except FileNotFoundError:
            return {}

    def _lock(self, jid, path):
        with self._guard:
            return self._files.setdefault((jid, path), threading.Lock())

    def ensure_file(self, jid, path):
        relative_path(path)
        with self._lock(jid, path):
            self._directories(jid, path)
            key = hashlib.sha256(path.encode()).hexdigest()
            receipt_path = jid + '/.collected/' + key + '.json'
            partial = jid + '/.collected/' + key + '.partial'
            target = jid + '/out/' + path
            previous = self._receipt(receipt_path)
            if previous.get('complete') and previous.get('final'):
                return
            final = self.completion_problem(jid) is None
            url = '/jobs/' + jid + '/out?' + urlencode({'path': path})
            info = json.loads(self.request(jid, url + '&meta=1')['body'])
            if previous.get('complete') and (previous['size'], previous['sha256']) == (info['size'], info['sha256']):
                self.store.write_json(receipt_path, {**previous, 'final': final})
                return
            same = previous.get('size') == info['size'] and previous.get('sha256') == info['sha256']
            if not same:
                try:
                    self.store.unlink(partial)
                except FileNotFoundError:
                    pass
            digest, offset = hashlib.sha256(), 0
            try:
                with self.store.open_file(partial) as stream:
                    for block in iter(lambda: stream.read(CHUNK), b''):
                        digest.update(block)
                        offset += len(block)
            except FileNotFoundError:
                with self.store.create_file(partial):
                    pass
            if offset >= info['size'] and (offset != info['size'] or digest.hexdigest() != info['sha256']):
                # A complete but corrupt transfer is not a resumable prefix.
                self.store.unlink(partial)
                with self.store.create_file(partial):
                    pass
                digest, offset = hashlib.sha256(), 0
            receipt = {'path': path, 'size': info['size'], 'sha256': info['sha256'],
                       'received_bytes': offset, 'complete': False, 'final': final, 'error': None}
            self.store.write_json(receipt_path, receipt)
            try:
                with self.store.append_file(partial) as stream:
                    while offset < info['size']:
                        end = min(info['size'], offset + CHUNK)
                        response = self.request(jid, url, headers={'Range': f'bytes={offset}-{end - 1}',
                            'If-Range': info['etag']}, maximum=CHUNK)
                        block = response['body']
                        if response['status'] != 206 or len(block) != end - offset:
                            raise OSError('artifact range was interrupted or changed')
                        stream.write(block)
                        stream.flush()
                        digest.update(block)
                        offset = end
                        receipt['received_bytes'] = offset
                        self.store.write_json(receipt_path, receipt)
                    os.fsync(stream.fileno())
                if offset != info['size'] or digest.hexdigest() != info['sha256']:
                    raise OSError('artifact bytes differ from the original size or SHA-256')
                self.store.replace(partial, target)
                self.store.sync(target, directory=False)
                self.store.write_json(receipt_path, {**receipt, 'complete': True})
            except (OSError, ValueError) as error:
                self.store.write_json(receipt_path, {**receipt, 'error': str(error)})
                raise

    def open_output(self, jid, path):
        if self.center.remote(jid):
            self.ensure_file(jid, path)
        return self.store.open_file(jid + '/out/' + path)

    def ensure_output(self, jid, path):
        if not path:
            return self.collect_tree(jid)
        relative_path(path)
        key = hashlib.sha256(path.encode()).hexdigest()
        receipt = self._receipt(jid + '/.collected/' + key + '.json')
        if receipt.get('complete') and receipt.get('final'):
            return
        parent = path.rpartition('/')[0]
        query = {'path': [parent], 'limit': ['100']}
        while True:
            page = self.list(jid, query)
            entry = next((item for item in page['items'] if item['path'] == path), None)
            if entry:
                if entry['kind'] == 'directory':
                    return self.collect_tree(jid, path)
                return self.ensure_file(jid, path)
            if page['next_cursor'] is None:
                raise FileNotFoundError(path)
            query['cursor'] = [page['next_cursor']]

    def list(self, jid, query):
        from node_read import artifact_page
        record = self.center.remote(jid)
        if record and (record.get('artifacts_sync') or {}).get('status') != 'complete':
            return json.loads(self.request(jid, '/jobs/' + jid + '/artifacts?' + urlencode(query, doseq=True))['body'])
        return artifact_page(self.store, jid, query)

    def sync_status(self, jid):
        record = self.center.remote(jid)
        if record is None:
            self.require_job(jid)
            return {'status': 'local', 'files': []}
        files = []
        try:
            directory = self.store.open_fd(jid + '/.collected', directory=True)
        except FileNotFoundError:
            directory = None
        if directory is not None:
            try:
                for name in sorted(os.listdir(directory)):
                    if name.endswith('.json') and name != 'inputs.json':
                        files.append(self.store.read_json(jid + '/.collected/' + name))
            finally:
                os.close(directory)
        return {**record.get('artifacts_sync', {'status': 'pending'}), 'files': files}

    def inputs(self, jid):
        path = jid + '/.collected/inputs.json'
        try:
            return self.store.read_json(path)
        except FileNotFoundError:
            pass
        value = json.loads(self.request(jid, '/jobs/' + jid + '/inputs')['body'])
        value.update(node_id=self.executor.node_id, execution_node_id=self.center.remote(jid)['execution_node_id'])
        if self.completion_problem(jid) is None:
            self._directories(jid, '')
            self.store.write_json(path, value)
        return value

    def collect_tree(self, jid, path=''):
        self._directories(jid, path + '/_')
        cursor = None
        while True:
            query = {'path': [path], 'limit': ['100']}
            if cursor:
                query['cursor'] = [cursor]
            page = self.list(jid, query)
            for item in page['items']:
                if item['kind'] == 'directory':
                    self.collect_tree(jid, item['path'])
                elif item['kind'] == 'file':
                    self.ensure_file(jid, item['path'])
                else:
                    raise ValueError('artifact is not a regular file or directory: ' + item['path'])
            cursor = page['next_cursor']
            if cursor is None:
                return

    def collect(self, jid):
        with self._lock(jid, ''):
            self._collect(jid)

    def _collect(self, jid):
        try:
            problem = self.completion_problem(jid)
            if problem:
                return
            self.center.targets.update(jid, artifacts_sync={'status': 'copying', 'at': time.time(), 'error': None})
            self.collect_tree(jid)
            source = self.source(jid)
            self.store.write_json(jid + '/meta.json', source['job'])
            self.inputs(jid)
            # A finished log has no further producer; copy bounded ranges as binary.
            temporary = jid + '/.collected/log.partial'
            try:
                self.store.unlink(temporary)
            except FileNotFoundError:
                pass
            with self.store.create_file(temporary) as stream:
                offset = 0
                while True:
                    block = self.request(jid, f'/jobs/{jid}/log?offset={offset}&limit={CHUNK}', maximum=CHUNK)['body']
                    stream.write(block)
                    offset += len(block)
                    if len(block) < CHUNK:
                        break
                stream.flush()
                os.fsync(stream.fileno())
            self.store.replace(temporary, jid + '/log')
            self.store.sync(jid + '/log', directory=False)
            self.center.targets.update(jid, artifacts_sync={'status': 'complete', 'at': time.time(), 'error': None})
        except (OSError, ValueError) as error:
            self.center.targets.update(jid, artifacts_sync={'status': 'error', 'at': time.time(), 'error': str(error)})

    def recollect(self, jid):
        # A recovery writes new reports into its original session directory.
        # Unchanged files keep their inode and existing result-source bindings.
        with self._lock(jid, ''):
            for receipt in self.sync_status(jid)['files']:
                with self._lock(jid, receipt['path']):
                    key = hashlib.sha256(receipt['path'].encode()).hexdigest()
                    path = jid + '/.collected/' + key + '.json'
                    self.store.write_json(path, {**self._receipt(path), 'final': False})
            record = self.center.targets.update(jid, artifacts_sync={'status': 'pending'})
        self.observed(record)

    def observed(self, record):
        status = record.get('observation') or {}
        if (status.get('state') != 'finished' or status.get('queue', {}).get('state') != 'done'
                or (record.get('artifacts_sync') or {}).get('status') == 'complete'):
            return
        jid = record['id']

        def run():
            try:
                self.collect(jid)
            finally:
                with self._guard:
                    self._threads.pop(jid)

        with self._guard:
            if self._closed or jid in self._threads:
                return
            thread = threading.Thread(target=run, daemon=True, name='job-artifacts')
            self._threads[jid] = thread
            thread.start()

    def close(self):
        with self._guard:
            self._closed = True
            threads = list(self._threads.values())
        for thread in threads:
            thread.join()
