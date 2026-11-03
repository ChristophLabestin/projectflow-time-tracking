#!/usr/bin/env python3
"""Configure the bundled recorder without exposing credentials to an agent/chat."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError

import agent_time as timer


def inspect(args):
    if not timer.git(args.cwd, 'rev-parse', '--show-toplevel'):
        raise ValueError('Choose the working directory of a Git repository.')
    root, canonical = timer.repo_roots(args.cwd)
    target = {'version': 1, 'tenantId': args.tenant_id,
              'defaultProjectId': args.project_id, 'apiBase': timer.validate_api(args.api_base)}
    config = root / '.projectflow/project.json'
    file_binding = json.loads(config.read_text()) if config.exists() else None
    registered = None
    db = timer.data_dir() / 'time.sqlite3'
    if db.exists():
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bindings'").fetchone():
                row = con.execute('SELECT body FROM bindings WHERE root=?', (str(canonical),)).fetchone()
                if row:
                    registered = json.loads(row[0])
    def same(binding):
        return all(binding.get(k) == target[k] for k in ('tenantId', 'defaultProjectId')) and ('apiBase' not in binding or binding['apiBase'] == target['apiBase'])
    conflict = any(binding and not same(binding) for binding in (registered, file_binding))
    def metadata(binding):
        return {k: binding.get(k) for k in ('tenantId', 'defaultProjectId')} if binding else None
    hooks = {}
    for client, folder, name in [('codex', '.codex', 'hooks.json'), ('claude', '.claude', 'settings.json')]:
        path = Path.home() / folder / name
        contents = json.loads(path.read_text()) if path.exists() else {}
        hooks[client] = sum('projectflow-agent-time' in hook.get('command', '') and f'--agent {client}' in hook.get('command', '')
                            for groups in contents.get('hooks', {}).values() for group in groups for hook in group.get('hooks', []))
    return {'root': str(root), 'canonicalRoot': str(canonical), 'fileBinding': metadata(file_binding),
            'localBinding': metadata(registered), 'registered': bool(registered and same(registered)),
            'conflict': conflict, 'credentialAvailable': bool(timer.token_for(target, explicit=True)),
            'hookHandlers': hooks}, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'configure'])
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--tenant-id', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--api-base', default=timer.DEFAULT_API)
    parser.add_argument('--client', choices=['both', 'codex', 'claude'], default='both')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--prompt-token', action='store_true')
    args = parser.parse_args()
    state, target = inspect(args)
    if args.action == 'inspect':
        print(json.dumps(state)); return
    if state['conflict'] and not args.replace:
        raise ValueError('Another project is linked. Confirm the intended change before using --replace.')
    if args.prompt_token:
        if not sys.stdin.isatty():
            raise ValueError('Enter the token in your own interactive terminal, not an agent tool.')
        token = getpass.getpass('ProjectFlow API token (hidden): ').strip()
        if not token:
            raise ValueError('No token entered.')
        os.environ['PROJECTFLOW_API_TOKEN'] = token
    if not timer.token_for(target, explicit=True):
        raise ValueError('Local credential missing. Use --prompt-token in your own terminal once.')
    policy = timer.api(target, 'GET', '/project-binding', token=timer.token_for(target, explicit=True))
    if not policy.get('enabled'):
        raise ValueError('Enable this project through MCP set_agent_time_tracking first.')
    args.save_token = bool(os.environ.get('PROJECTFLOW_API_TOKEN'))
    timer.link(args)
    for client in ('codex', 'claude') if args.client == 'both' else (args.client,):
        timer.install(argparse.Namespace(client=client))
    print(json.dumps({'configured': True, 'state': inspect(args)[0],
                      'next': 'Verify native hook trust, then a real subsequent prompt upload.'}))


if __name__ == '__main__':
    try:
        main()
    except HTTPError as exc:
        print(json.dumps({'configured': False, 'httpStatus': exc.code,
                          'error': 'ProjectFlow rejected setup; check local credential, role and project scope.'}), file=sys.stderr)
        sys.exit(2)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'configured': False, 'error': str(exc)}), file=sys.stderr)
        sys.exit(2)
