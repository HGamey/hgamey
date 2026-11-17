"""A job's upload either reaches the computer that runs it whole or the submitter is told why not: a cut upload starts
no job, one too large to send to another computer is refused before it is read, and the other computer fetches it from
the center in pieces, fetching a dropped piece again, while the center waits longer for a larger upload. A job handed
to the other computer that never arrived is finished by a cancel or reset instead of staying unknown."""
import hashlib
import http.client
import io
import json
import os
import secrets
import socket
import sys
import tarfile
import time
import unittest
from unittest.mock import patch

import test_multi_node_jobs as multi


def archive(payload):
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode='w:gz', compresslevel=1) as tar:
        info = tarfile.TarInfo('blob.bin')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return body.getvalue()


CHECKSUM = ['-c', "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())", '@in/blob.bin']


class JobUploadTransferTests(unittest.TestCase):
    setUp = multi.MultiNodeJobTests.setUp
    start_worker, cli = multi.MultiNodeJobTests.start_worker, multi.MultiNodeJobTests.cli

    def raw(self, spec, key, length, sent=b''):
        """POST /jobs declaring `length` bytes, sending only `sent`, then ending the upload."""
        with socket.create_connection(('127.0.0.1', self.center.server.server_port), timeout=10) as connection:
            connection.sendall(f'POST /jobs HTTP/1.1\r\nHost: center\r\nAuthorization: Bearer {self.agent}\r\n'
                               f'X-Job: {json.dumps(spec)}\r\nX-Request-ID: {key}\r\n'
                               f'Content-Length: {length}\r\n\r\n'.encode() + sent)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def post(self, spec, key, body):
        connection = http.client.HTTPConnection('127.0.0.1', self.center.server.server_port, timeout=60)
        try:
            connection.request('POST', '/jobs', body=body, headers={
                'Authorization': 'Bearer ' + self.agent, 'X-Job': json.dumps(spec), 'X-Request-ID': key})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_an_upload_cut_short_starts_no_job(self):
        self.center.jobs.bins['fixture'] = (sys.executable, str(self.center.root))
        spec = {'bin': 'fixture', 'argv': CHECKSUM, 'device_access': 'none'}
        code, reply = self.raw(spec, 'cut-upload', 1000, b'x' * 100)
        self.assertEqual((code, reply.get('code')), (400, 'upload_incomplete'), reply)
        self.assertIn('收到 100 / 声明 1000 字节', reply['error'])
        self.assertEqual(self.center.jobs.center.job_ids(), set())

    def test_an_upload_too_large_for_another_computer_is_refused_before_it_is_read(self):
        self.start_worker()
        spec = {'bin': 'fixture', 'argv': CHECKSUM, 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        code, reply = self.raw(spec, 'too-large', (64 << 20) + 1)
        self.assertEqual((code, reply.get('code')), (413, 'input_too_large'), reply)
        self.assertIn('最多 64 MiB，这次 64.0 MiB', reply['error'])
        self.assertEqual(self.center.jobs.center.job_ids(), set())

    def test_a_dropped_piece_is_fetched_again_and_a_larger_upload_is_waited_for(self):
        connector = self.start_worker()
        payload = os.urandom(2 << 20)
        body, gets = archive(payload), []
        original = connector._send

        def flaky(method, path, *args, **kwargs):
            if method == 'GET' and '/body' in path:
                gets.append(path)
                time.sleep(0.3)
                if len(gets) == 2:
                    raise http.client.IncompleteRead(b'', 1)
            return original(method, path, *args, **kwargs)

        connector._send = flaky
        spec = {'bin': 'fixture', 'argv': CHECKSUM, 'device_access': 'none',
                'execution_node_id': self.worker.jobs.node_id}
        with patch('node_connector.BODY_PIECE', 256 << 10, create=True), patch('job_center.DEADLINE_S', 1), \
                patch('job_center.DELIVERY_RATE', 256 << 10, create=True):
            code, job = self.post(spec, 'flaky-transfer', body)
        self.assertEqual(code, 200, job)
        self.assertNotEqual(job['phase'], 'submission_unknown', job)
        self.assertGreater(len(gets), len(body) // (256 << 10) + 1)
        deadline = time.monotonic() + 30
        while self.worker.jobs.status(job['id'])['state'] != 'finished' and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.worker.jobs.status(job['id'])['outcome'], 'succeeded', self.worker.jobs.log_from(job['id'], 0, 4096))
        self.assertIn(hashlib.sha256(payload).hexdigest().encode(), self.worker.jobs.log_from(job['id'], 0, 4096))

    def test_an_upload_the_other_computer_cannot_fetch_is_reported_to_the_submitter(self):
        connector = self.start_worker()
        source = self.center.root / 'blob.bin'
        source.write_bytes(os.urandom(1 << 20))
        original = connector._send

        def broken(method, path, *args, **kwargs):
            if method == 'GET' and '/body' in path and 'offset=0&' not in path:
                raise http.client.IncompleteRead(b'', 1)
            return original(method, path, *args, **kwargs)

        connector._send = broken
        with patch('node_connector.BODY_PIECE', 256 << 10, create=True), \
                patch('node_connector.BODY_ATTEMPTS', 2, create=True):
            result = self.cli('--execution-node', self.worker.jobs.node_id, '--request-id', 'broken-transfer',
                              '--detach', 'fixture', '-c', 'pass', str(source))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('输入没有传到执行节点，任务没有开始，request_id=broken-transfer', result.stderr)
        self.assertRegex(result.stderr, r'执行节点只收到随作业上传的输入的 262144 / \d+ 字节')
        self.assertIn('加 --request-id broken-transfer 再提交一次会重新传', result.stderr)
        self.assertEqual(self.worker.jobs.center.job_ids(), set())

    def test_a_job_the_other_computer_never_received_is_finished_by_a_cancel_or_reset(self):
        self.start_worker()
        center = self.center.jobs.center
        for action, body in [('cancel', {}), ('reset', {'note': 'upload never arrived'})]:
            jid = center.targets.reserve({'id': secrets.token_hex(6), 'request_id': 'lost-' + action,
                'fingerprint': action, 'execution_node_id': self.worker.jobs.node_id,
                'request': {'spec': {'bin': 'fixture', 'argv': CHECKSUM, 'device_access': 'none'}},
                'submitted_at': time.time(), 'device_selection': None, 'sent_at': time.time(), 'stop': None,
                'observation': None})['id']
            self.assertEqual(self.center.call('GET', '/jobs/' + jid)[1]['phase'], 'submission_unknown')
            code, receipt = self.center.call('POST', f'/jobs/{jid}/{action}', body)
            self.assertEqual((code, receipt['pending']), (200, False), receipt)
            code, job = self.center.call('GET', '/jobs/' + jid)
            self.assertEqual((job['state'], job['outcome'], job['queue']['state']), ('finished', 'cancelled', 'done'))
            self.assertEqual(self.worker.jobs.center.job_ids(), set())


if __name__ == '__main__':
    unittest.main()
