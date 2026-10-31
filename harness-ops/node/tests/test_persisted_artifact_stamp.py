"""A remount can renumber st_dev without changing registered source bytes."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_node_evaluation as evaluation
import test_game_upload_cache as uploads
from game_assets import AssetCache


@contextmanager
def remounted_device(number=1):
    fstat = os.fstat

    def changed(fd):
        observed = fstat(fd)
        fields = {key: getattr(observed, key) for key in dir(observed) if key.startswith('st_')}
        fields['st_dev'] += number
        return SimpleNamespace(**fields)

    with patch('os.fstat', side_effect=changed):
        yield


class RegisteredMediaRemountTests(unittest.TestCase):
    def setUp(self):
        self.env = evaluation.NodeEvaluationTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)

    def test_registered_attachment_and_job_media_survive_device_renumbering(self):
        env = self.env
        raw = evaluation.png()
        aid = 'a' * 32
        env.env.jobs.research.upload_attachment(aid, 'image.png', 'image/png', raw)
        (env.out / 'image.png').write_bytes(raw)
        sources = [{'name': 'attachment', 'kind': 'attachment', 'attachment_id': aid,
                    'filename': 'image.png', 'local_path': '/fixture/image.png'},
                   {'name': 'output', 'kind': 'job_output', 'job_id': env.jid, 'path': 'image.png'}]
        env.register('remount', sources)
        before = env.env.jobs.results._row('remount')['observations']
        env.create('research', 'study', {'title': 'Remount', 'objective': 'Read the original media'})
        env.create('request', 'remount', env.question([env.ref('attachment', 'remount'), env.ref('output', 'remount')],
                                                    slots={'A': 0, 'B': 1}))
        paths = [Path(env.env.jobs.data_root) / '_research/attachments' / aid, env.out / 'image.png']
        with remounted_device():
            for name, slot, path in zip(('attachment', 'output'), ('A', 'B'), paths):
                with self.subTest(source=name):
                    media = f'/research-state/request/remount/media/{slot}?revision=1'
                    code, headers, body = env.request('GET', media, token=env.ro)
                    self.assertEqual((code, body), (200, raw))
                    self.assertEqual(headers['Content-Type'], 'image/png')
                    detail = env.api('GET', '/results/remount')[1]
                    current = next(s for s in detail['current']['sources'] if s['name'] == name)
                    self.assertEqual(current['status'], 'available')
                    code, _, body = env.request('GET', f'/results/remount/sources/{name}', token=env.ro,
                                                headers={'Range': 'bytes=0-7'})
                    self.assertEqual((code, body), (206, raw[:8]))
                    # Same size and restored mtime must not hide a real content edit.
                    original = path.stat()
                    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                    code, _, body = env.request('GET', media, token=env.ro)
                    self.assertEqual((code, json.loads(body)['code']), (409, 'result_source_changed'))
                    detail = env.api('GET', '/results/remount')[1]
                    current = next(s for s in detail['current']['sources'] if s['name'] == name)
                    self.assertEqual(current['status'], 'changed')
        self.assertEqual(env.env.jobs.results._row('remount')['observations'], before)


class AssetRemountTests(unittest.TestCase):
    def test_sealed_upload_survives_device_renumbering(self):
        with tempfile.TemporaryDirectory() as root:
            cache = AssetCache(Path(root) / 'cache')
            payload = b'original uploaded bytes'
            item = uploads.asset(payload)
            cache.upload_open(item, 'upload')
            cache.upload_chunk(item, 'upload', 0, len(payload), io.BytesIO(payload))
            cache.upload_seal(item, 'upload')
            cache = AssetCache(cache.root)
            with remounted_device():
                with self.subTest(stage='sealed status'):
                    self.assertEqual(cache.upload_status(item, 'upload')['state'], 'sealed')
                with self.subTest(stage='verification'):
                    self.assertEqual(cache.verify_upload(item, 'upload')['state'], 'verified')
                    self.assertEqual(Path(cache.object_path(item['sha256'])).read_bytes(), payload)

    def test_verified_receipt_survives_device_renumbering(self):
        with tempfile.TemporaryDirectory() as root:
            cache = AssetCache(Path(root) / 'cache')
            payload = b'original uploaded bytes'
            item = uploads.asset(payload)
            cache.upload_open(item, 'upload')
            cache.upload_chunk(item, 'upload', 0, len(payload), io.BytesIO(payload))
            cache.upload_seal(item, 'upload')
            self.assertEqual(cache.verify_upload(item, 'upload')['state'], 'verified')
            cache = AssetCache(cache.root)
            with remounted_device():
                with patch.object(cache, '_digest', side_effect=AssertionError('cached query rehashed')):
                    with self.subTest(stage='verified receipt'):
                        self.assertEqual(cache.observe(item)['state'], 'verified')
                        self.assertEqual(cache.upload_status(item, 'upload')['state'], 'verified')
                blob = Path(cache.object_path(item['sha256']))
                blob.chmod(0o600)
                blob.write_bytes(b'x' * len(payload))
                self.assertEqual(cache.observe(item)['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
