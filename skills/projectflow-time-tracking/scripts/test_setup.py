"""Isolated setup tests; no real credentials, hooks or ProjectFlow writes."""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.enabled = True
        self.secret = 'synthetic-test-token-not-a-real-credential'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer ' + owner.secret:
                    self.send_response(401); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'enabled': owner.enabled, 'title': 'Fixture', 'binding': {
                    'version': 1, 'tenantId': 'test-tenant', 'defaultProjectId': 'p1'}}).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api = f'http://127.0.0.1:{self.server.server_port}/api'
        self.env = patch.dict(os.environ, {'PROJECTFLOW_TIME_HOME': str(self.base / 'projectflow-agent-time'),
                                          'PROJECTFLOW_API_TOKEN': self.secret})
        self.env.start()
        self.home = patch.object(Path, 'home', return_value=self.base / 'user')
        self.home.start()

    def tearDown(self):
        self.home.stop(); self.env.stop()
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()

    def run_setup(self, *extra, cwd=None, action='configure'):
        argv = ['setup.py', action, '--cwd', str(cwd or self.repo), '--tenant-id', 'test-tenant',
                '--project-id', 'p1', '--api-base', self.api, *extra]
        output = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output):
            setup.main()
        self.assertNotIn(self.secret, output.getvalue())
        return json.loads(output.getvalue().splitlines()[-1])

    def test_real_http_setup_preserves_hooks_is_idempotent_and_registers_worktrees(self):
        config = self.base / 'user/.claude/settings.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'existingSetting': True, 'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'echo existing'}]}]}}))
        self.assertTrue(self.run_setup()['configured'])
        again = self.run_setup()['state']
        self.assertFalse(again['conflict'])
        self.assertEqual(again['hookHandlers'], {'codex': 4, 'claude': 3})
        self.assertTrue(json.loads(config.read_text())['existingSetting'])
        self.assertIn('echo existing', config.read_text())
        credential = next((self.base / 'projectflow-agent-time/credentials').glob('*.json'))
        self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.secret, (self.repo / '.projectflow/project.json').read_text())
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                        'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        tree = self.base / 'tree'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-q', '-b', 'test-tree', str(tree)], check=True)
        with patch.dict(os.environ, {'PROJECTFLOW_API_TOKEN': ''}):
            state = self.run_setup(cwd=tree, action='inspect')
        self.assertTrue(state['registered'])
        self.assertTrue(state['credentialAvailable'])
        self.assertEqual(state['canonicalRoot'], str(self.repo.resolve()))
        # Metadata absent in the new worktree must not permit an implicit project change.
        with setup.timer.database() as con:
            con.execute('UPDATE bindings SET body=?', (json.dumps({'tenantId': 'other', 'defaultProjectId': 'other'}),))
        with self.assertRaisesRegex(ValueError, 'Another project'):
            self.run_setup(cwd=tree)

    def test_missing_credentials_and_disabled_policy_do_not_write_hooks_or_binding(self):
        with patch.dict(os.environ, {'PROJECTFLOW_API_TOKEN': ''}):
            with self.assertRaisesRegex(ValueError, 'credential missing'):
                self.run_setup()
        self.enabled = False
        with self.assertRaisesRegex(ValueError, 'Enable this project'):
            self.run_setup()
        self.assertFalse((self.repo / '.projectflow/project.json').exists())
        self.assertFalse((self.base / 'user/.codex/hooks.json').exists())

    def test_reject_non_git_directories_and_noninteractive_secret_input(self):
        with self.assertRaisesRegex(ValueError, 'Git repository'):
            self.run_setup(cwd=self.base)
        with patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'own interactive terminal'):
                self.run_setup('--prompt-token')


if __name__ == '__main__':
    unittest.main()
