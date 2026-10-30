"""Portable payload and launcher boundaries; no provider requests."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import desktop_entry as entry
import desktop_launcher as launcher
import build_desktop
import restart_checker


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='Orkestryn desktop ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, files=None, extra=None):
        files = {'desktop_entry.py': b"PAYLOAD_SHA256 = '__ORK_PAYLOAD_SHA256__'\n", 'desktop_launcher.py': b'# fixture\n'} if files is None else files
        manifest = {'package_version': 'test', 'file_sha256': {n: hashlib.sha256(v).hexdigest() for n, v in files.items()}}
        data = {**files, 'bundle_manifest.json': json.dumps(manifest).encode()}
        data.update(extra or {})
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for n, v in data.items():
                archive.writestr(n, v)
        raw = stream.getvalue()
        return raw, hashlib.sha256(raw).hexdigest(), data

    def test_complete_payload_extracts_and_cache_reuses_exact_bytes(self):
        raw, digest, files = self.archive()
        target = entry.unpack(raw, self.root / 'cache with spaces', digest)
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, files)
        self.assertEqual(entry.unpack(raw, target.parent, digest), target)

    def test_changed_cache_is_rejected_without_overwriting(self):
        raw, digest, _ = self.archive()
        target = entry.unpack(raw, self.root, digest)
        (target / 'desktop_launcher.py').write_text('changed')
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, digest)
        self.assertEqual((target / 'desktop_launcher.py').read_text(), 'changed')

    def test_wrong_payload_hash_creates_no_cache(self):
        raw, _, _ = self.archive()
        cache = self.root / 'absent'
        with self.assertRaises(ValueError):
            entry.unpack(raw, cache, '0' * 64)
        self.assertFalse(cache.exists())

    def test_unlisted_cached_module_is_rejected(self):
        raw, digest, _ = self.archive()
        target = entry.unpack(raw, self.root, digest)
        (target / 'ssl.py').write_text('# unlisted fixture')
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, digest)

    def test_zip_traversal_is_rejected_before_writing(self):
        raw, digest, _ = self.archive(extra={'../outside.py': b'fixture'})
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, digest)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unlisted_payload_is_rejected(self):
        raw, digest, _ = self.archive(extra={'unexpected.py': b'fixture'})
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, digest)

    def test_changed_file_hash_is_rejected(self):
        raw, _, files = self.archive()
        manifest = json.loads(files['bundle_manifest.json'])
        manifest['file_sha256']['desktop_launcher.py'] = 'f' * 64
        raw, digest, _ = self.archive(extra={'bundle_manifest.json': json.dumps(manifest).encode()})
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, digest)

    def test_zip_link_is_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            info = zipfile.ZipInfo('linked.py');info.external_attr = 0o120777 << 16
            archive.writestr(info, 'elsewhere')
        raw = stream.getvalue()
        with self.assertRaises(ValueError):
            entry.unpack(raw, self.root, hashlib.sha256(raw).hexdigest())

    def test_worker_path_outside_verified_code_is_rejected(self):
        with self.assertRaises(ValueError):
            entry.worker_dispatch(self.root, ['-B', '-S', str(self.root.parent / 'durable_runtime.py')])

    def test_non_worker_script_is_rejected(self):
        with self.assertRaises(ValueError):
            entry.worker_dispatch(self.root, ['-u', str(self.root / 'project.py')])

    def test_worker_dispatch_keeps_internal_arguments(self):
        script = self.root / 'durable_runtime.py'
        with mock.patch.object(entry.runpy, 'run_path') as run, mock.patch.object(sys, 'argv', []):
            entry.worker_dispatch(self.root, ['-B', '-S', str(script), '--worker', 'fixture'])
            run.assert_called_once_with(str(script), run_name='__main__')
            self.assertEqual(sys.argv, [str(script), '--worker', 'fixture'])

    def test_frozen_worker_uses_same_executable(self):
        with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, '_base_executable', 'missing-build-machine-python.exe', create=True), mock.patch.object(sys, 'dont_write_bytecode', False), mock.patch.object(restart_checker.platform, 'system', return_value='Windows'):
            entry.configure_runtime()
            self.assertEqual(Path(restart_checker.worker_interpreter()).resolve(), Path(sys.executable).resolve())

    def test_judge_launcher_does_not_read_owner_key(self):
        with mock.patch.dict(os.environ, {'NEBIUS_API_KEY': 'LOCAL_OWNER_KEY_FIXTURE'}), mock.patch.object(launcher.downloadable_demo, 'prepare_demo', return_value=('server', 'guide', 'version')) as demo, mock.patch.object(launcher.browser_proposals, 'providers') as clients:
            self.assertEqual(launcher.prepare(False, self.root, 0), ('server', 'guide', 'version'))
            demo.assert_called_once_with(self.root, port=0)
            clients.assert_not_called()

    def test_owner_client_is_constructed_without_request(self):
        with mock.patch.dict(os.environ, {'NEBIUS_API_KEY': 'LOCAL_OWNER_KEY_FIXTURE'}), mock.patch.object(launcher.install_update, 'payload', return_value=({}, 'version')), mock.patch.object(launcher.operations_server, 'Server', return_value='server') as server, mock.patch.object(launcher.browser_proposals.RoleClient, 'ask', side_effect=AssertionError('No startup inference')):
            result = launcher.prepare(True, self.root, 0)
            self.assertEqual(result, ('server', None, 'version'))
            self.assertEqual(set(server.call_args.kwargs['proposal_providers']), {'factory', 'governance', 'recovery'})

    def test_owner_without_key_keeps_browser_entry(self):
        with mock.patch.dict(os.environ, {'NEBIUS_API_KEY': ''}), mock.patch.object(launcher.install_update, 'payload', return_value=({}, 'version')), mock.patch.object(launcher.operations_server, 'Server') as server:
            launcher.prepare(True, self.root, 0)
            self.assertIsNone(server.call_args.kwargs['proposal_providers'])

    def test_judge_data_path_is_outside_code_cache(self):
        with mock.patch.object(entry, 'private_root', return_value=self.root):
            self.assertEqual(launcher.data_directory(False), self.root / 'judge-workspace')
            self.assertNotEqual(launcher.data_directory(False), self.root / 'code')

    def test_zipapp_build_contains_only_pinned_public_payload(self):
        raw, _, files = self.archive()
        with mock.patch.object(build_desktop.install_update, 'payload', return_value=(files, 'test')), mock.patch.object(build_desktop.release_check, 'package_entries', return_value=[]):
            output = self.root / 'output' / 'Orkestryn.pyz'
            record = build_desktop.build(self.root / 'source', output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {'__main__.py', 'payload.zip'})
            payload = archive.read('payload.zip')
            self.assertIn(hashlib.sha256(payload).hexdigest().encode(), archive.read('__main__.py'))
        self.assertTrue(record['requires_python'])

    def test_builder_refuses_output_in_source(self):
        _, _, files = self.archive()
        with mock.patch.object(build_desktop.install_update, 'payload', return_value=(files, 'test')), mock.patch.object(build_desktop.release_check, 'package_entries', return_value=[]):
            with self.assertRaises(ValueError):
                build_desktop.build(self.root, self.root / 'Orkestryn.pyz')

    def test_builder_refuses_existing_output(self):
        _, _, files = self.archive()
        output = self.root / 'Orkestryn.pyz';output.write_text('existing')
        with mock.patch.object(build_desktop.install_update, 'payload', return_value=(files, 'test')), mock.patch.object(build_desktop.release_check, 'package_entries', return_value=[]):
            with self.assertRaises(ValueError):
                build_desktop.build(self.root / 'source', output)
        self.assertEqual(output.read_text(), 'existing')

    def test_builder_accepts_parent_relative_output(self):
        _, _, files = self.archive()
        source = self.root / 'source';source.mkdir()
        with mock.patch.object(build_desktop.install_update, 'payload', return_value=(files, 'test')), mock.patch.object(build_desktop.release_check, 'package_entries', return_value=[]):
            result = build_desktop.build(source, source / '..' / 'portable.pyz')
        self.assertEqual(result['file'], 'portable.pyz')
        self.assertTrue((self.root / 'portable.pyz').is_file())

    def test_finite_check_exit_status_blocks_failed_native_artifacts(self):
        args = SimpleNamespace(directory=self.root, main=False, check=True)
        for status, expected in [('PASSED', 0), ('FAILED', 1)]:
            with self.subTest(status=status), mock.patch.object(launcher.downloadable_demo, 'check_demo', return_value={'status': status}), mock.patch.object(launcher, 'prepare') as server, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.console(args), expected)
                server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
