"""Publication checks use invented values and temporary repositories only."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import release_check as release


class ReleaseTests(unittest.TestCase):
    def test_private_accounts_env_runs_and_keys_are_rejected(self):
        for name in ['.env', 'config/.env.local', '.control_access/accounts.sqlite3',
                     'operations_runs/one/summary.json', 'update_backups/source.py',
                     '.venv/tool.py', 'private.pem', 'accounts.db-wal']:
            with self.subTest(name=name):
                self.assertTrue(release.path_problem(name))

    def test_source_docs_and_workflow_are_allowed(self):
        for name in ['README.md', 'operations_proposals.py', '.github/workflows/ci.yml',
                     'examples/workflow.json', 'RELEASE_VERIFICATION.json']:
            self.assertIsNone(release.path_problem(name))

    def test_unsafe_paths_links_and_case_collisions_are_blocked(self):
        for name in ['../outside', '/absolute', 'a\\b', 'C:/outside', 'a/../b', 'a//b']:
            self.assertTrue(release.path_problem(name))
        self.assertEqual(len(release.check_entries([('a.py', b'ok', '120000')])), 1)
        self.assertEqual(len(release.check_entries([('A.py', b'ok', '100644'), ('a.py', b'ok', '100644')])), 1)

    def test_credentials_and_private_keys_are_found_without_echo(self):
        invented = ['sk' + '-' + 'aB7x' * 12, 'ghp' + '_' + 'aB7x' * 10,
                    'github_pat' + '_' + 'aB7x' * 12, '-----BEGIN ' + 'RSA PRIVATE KEY-----']
        for token in invented:
            result = release.check_entries([('config.py', token.encode(), '100644')])
            self.assertEqual(len(result), 1)
            self.assertNotIn(token, json.dumps(result))

    def test_long_key_literal_is_blocked_but_environment_lookup_is_allowed(self):
        value = 'invented-value-' * 4
        self.assertTrue(release.content_problem(('NEBIUS_API_KEY = "' + value + '"').encode()))
        self.assertIsNone(release.content_problem(b'key = os.environ.get("NEBIUS_API_KEY")'))
        self.assertIsNone(release.content_problem(b'api_key = key'))
        self.assertIsNone(release.content_problem(('a' * 64).encode()))

    def package(self, root):
        (root/'app.py').write_bytes(b'print("offline")\n')
        (root/'.github/workflows').mkdir(parents=True)
        (root/'.github/workflows/ci.yml').write_bytes(b'name: Offline checks\n')
        manifest = {'file_sha256': {'app.py': hashlib.sha256((root/'app.py').read_bytes()).hexdigest()},
                    'repository_files_sha256': {'.github/workflows/ci.yml': hashlib.sha256((root/'.github/workflows/ci.yml').read_bytes()).hexdigest()}}
        (root/'bundle_manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_package_verifies_extras_and_ignores_unlisted_local_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.package(root)
            (root/'.env').write_text('LOCAL_ONLY')
            self.assertEqual(len(release.package_entries(root)), 3)

    def test_tampered_payload_and_missing_extra_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.package(root)
            (root/'app.py').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                release.package_entries(root)
            self.package_reset(root)
            (root/'.github/workflows/ci.yml').unlink()
            with self.assertRaises(FileNotFoundError):
                release.package_entries(root)

    def package_reset(self, root):
        (root/'app.py').write_bytes(b'print("offline")\n')

    def test_manifest_cannot_include_private_or_escape_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['.env', '../outside.py']:
                (root/'bundle_manifest.json').write_text(json.dumps({'file_sha256': {name:'x'}}))
                with self.assertRaisesRegex(ValueError, 'Unsafe release path'):
                    release.package_entries(root)

    def test_staged_blob_is_checked_even_if_working_copy_was_cleaned(self):
        if not shutil.which('git'):
            self.skipTest('Git is required only for the index integration check.')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(['git','init','--quiet',str(root)],check=True)
            secret='ghp'+'_'+'aB8c'*10
            (root/'config.py').write_text(secret)
            subprocess.run(['git','-C',str(root),'add','config.py'],check=True)
            (root/'config.py').write_text('safe working copy')
            findings=release.check_entries(release.staged_entries(root))
            self.assertEqual([x['file'] for x in findings], ['config.py'])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(release.main(['--staged','--root',str(root)]),1)
            self.assertNotIn(secret,out.getvalue())
            subprocess.run(['git','-C',str(root),'add','config.py'],check=True)
            self.assertEqual(release.check_entries(release.staged_entries(root)),[])

    def test_empty_index_and_malformed_manifest_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'bundle_manifest.json').write_text('{bad')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(release.main(['--package','--root',str(root)]),1)
            self.assertIn('BLOCKED',out.getvalue())
            if shutil.which('git'):
                subprocess.run(['git','init','--quiet',str(root)],check=True)
                with self.assertRaisesRegex(ValueError,'empty'):
                    release.staged_entries(root)


if __name__ == '__main__':
    unittest.main()
