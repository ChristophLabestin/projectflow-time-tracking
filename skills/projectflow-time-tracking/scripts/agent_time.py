#!/usr/bin/env python3
"""Local agent timer with brief activity summaries and per-prompt token counts. No polling; from transcripts only usage numbers are read."""
from __future__ import annotations

import argparse
import contextlib
import csv
import glob
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib import request, error, parse
import uuid

ID = re.compile(r'[a-zA-Z0-9_.:-]{1,160}')
DEFAULT_API = 'https://app.getprojectflow.com/api'
EVENTS = ('UserPromptSubmit', 'Stop', 'Interrupt', 'SessionEnd')

SUMMARY_CONTEXT = ('ProjectFlow time tracking: end this turn with a plain line "ProjectFlow: <activity>". '
                   'Summarize only work actually done in 5-15 words, at most 160 characters, in the language of the user. '
                   'Mention incomplete/failed work honestly. No secrets, prompt quotations or private paths. '
                   'The hook captures only that line; no extra tool call is needed.')


def activity_summary(message):
    if not isinstance(message, str):
        return None
    matches = []
    for line in message.splitlines():
        match = re.fullmatch(r'\s*(?:\*\*)?ProjectFlow:(?:\*\*)?\s+(.+?)\s*', line)
        if match:
            text = ' '.join(match.group(1).split())
            if 2 <= len(text.split()) and len(text) <= 160 and all(c.isprintable() for c in text):
                matches.append(text)
    return matches[0] if len(matches) == 1 else None


def event_key(event, agent):
    return f"{agent}:{event.get('session_id')}:{event.get('turn_id') or 'current'}"


def repair_key(event, agent):
    return f"summaryRepair:{agent}:{event.get('session_id')}"


def describe(entry_id, description):
    summary = activity_summary('ProjectFlow: ' + description)
    if not summary:
        raise ValueError('Description must be a single brief activity (2+ words, at most 160 characters).')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT body FROM outbox WHERE id=? AND state='review' AND error='missing-description'", (entry_id,)).fetchone()
        if not row:
            return False
        body = json.loads(row['body']); body['description'] = summary
        con.execute("UPDATE outbox SET body=?,state='pending',error=NULL,next_try=0 WHERE id=?", (json.dumps(body), entry_id))
    return True


def process_event(event, agent='codex', clock=None):
    """One compact instruction; at most one repair continuation, never timed as new work."""
    name = event.get('hook_event_name')
    marker = repair_key(event, agent)
    with database() as con:
        row = con.execute('SELECT value FROM meta WHERE key=?', (marker,)).fetchone()
        repair = json.loads(row['value']) if row else None
        if name == 'UserPromptSubmit' and repair:
            if event.get('prompt', '').strip() == repair['reason']:
                repair['repairTurn'] = event.get('turn_id')
                con.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(repair), marker))
                return {}, False
            # A real new user prompt is never swallowed by an abandoned repair.
            con.execute('DELETE FROM meta WHERE key=?', (marker,))
            repair = None
        if name in ('SessionEnd', 'Interrupt'):
            con.execute('DELETE FROM meta WHERE key=?', (marker,))
        if name == 'Stop' and repair and (event.get('stop_hook_active') or
                (repair.get('repairTurn') and repair['repairTurn'] == event.get('turn_id'))):
            con.execute('DELETE FROM meta WHERE key=?', (marker,))
            con.commit()
            summary = activity_summary(event.get('last_assistant_message'))
            if summary and describe(repair['entryId'], summary):
                return {}, True
            return {'systemMessage': 'ProjectFlow: activity summary still missing. Time retained locally for review; no further retry.'}, False
    output = {}
    queued = record(event, agent, clock, context=output)
    if name == 'UserPromptSubmit':
        return output, queued
    with database() as con:
        row = con.execute('SELECT body,state FROM runs WHERE key=?', (event_key(event, agent),)).fetchone()
        if not row:
            return {}, queued
        if name == 'Stop' and row['state'] == 'closed':
            entry_id = json.loads(row['body'])['entryId']
            held = con.execute("SELECT 1 FROM outbox WHERE id=? AND state='review' AND error='missing-description'", (entry_id,)).fetchone()
            attempted = 'summaryAttempt:' + entry_id
            if held and not con.execute('SELECT 1 FROM meta WHERE key=?', (attempted,)).fetchone():
                con.execute('INSERT INTO meta VALUES (?,?)', (attempted, '1'))
                if not event.get('stop_hook_active'):
                    reason = (f'ProjectFlow activity summary required ({entry_id}). '
                              'Reply only with "ProjectFlow: <what you actually did>" in the language of the user, '
                              '5-15 words, at most 160 characters. State incomplete work honestly; no secrets or prompt quotations. '
                              'Do not run tools or perform more work. The timer has already stopped.')
                    con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                (marker, json.dumps({'entryId': entry_id, 'reason': reason})))
                    return {'decision': 'block', 'reason': reason}, queued
    return {}, queued


def data_dir():
    p = Path(os.environ.get('PROJECTFLOW_TIME_HOME', str(Path.home() / '.local/share/projectflow-agent-time')))
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


@contextlib.contextmanager
def database():
    os.umask(0o077)
    con = sqlite3.connect(data_dir() / 'time.sqlite3', timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS bindings(root TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(key TEXT PRIMARY KEY, binding TEXT NOT NULL, body TEXT NOT NULL,
          started REAL NOT NULL, monotonic REAL NOT NULL, boot TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, binding TEXT NOT NULL, body TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_try REAL NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    try:
        yield con
        con.commit()
    finally:
        con.close()


def git(cwd, *args):
    try:
        return subprocess.check_output(['git', '-C', str(cwd), *args], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return ''


def repo_roots(cwd):
    root = Path(git(cwd, 'rev-parse', '--show-toplevel') or cwd).resolve()
    common = git(root, 'rev-parse', '--git-common-dir')
    canonical = (root / common).resolve().parent if common else root
    return root, canonical


def atomic_json(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(body, indent=2) + '\n', encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


def validate_api(value):
    url = parse.urlparse(value)
    if url.username or url.password or url.query or url.fragment:
        raise ValueError('API URL must not contain credentials, a query or a fragment.')
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('API requires HTTPS (HTTP is allowed only for local emulators).')
    return value.rstrip('/')


def profile_key(binding):
    return hashlib.sha256((binding['apiBase'] + '\n' + binding['tenantId']).encode()).hexdigest()


def token_for(binding, explicit=False):
    # Repository files cannot redirect an existing credential to a different host/workspace.
    if explicit and os.environ.get('PROJECTFLOW_API_TOKEN'):
        return os.environ['PROJECTFLOW_API_TOKEN']
    p = data_dir() / 'credentials' / (profile_key(binding) + '.json')
    if p.exists():
        return json.loads(p.read_text())['token']
    return ''


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(binding, method, suffix, body=None, token=None, timeout=10):
    token = token if token is not None else token_for(binding)
    if not token:
        raise ValueError('No local credential. Link with --save-token using PROJECTFLOW_API_TOKEN.')
    url = binding['apiBase'] + '/projectflow/projects/' + parse.quote(binding['defaultProjectId'], safe='') + suffix
    sep = '&' if '?' in url else '?'
    url += sep + parse.urlencode({'tenantId': binding['tenantId']})
    req = request.Request(url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        return json.load(response)


def refresh_session_policy(event):
    """One small policy read per session, never one per prompt. Offline cache expires after a day."""
    if event.get('hook_event_name') != 'UserPromptSubmit' or not event.get('session_id'):
        return
    _, canonical = repo_roots(event.get('cwd') or os.getcwd())
    marker = 'policy:' + hashlib.sha256((str(canonical) + str(event['session_id'])).encode()).hexdigest()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT body FROM bindings WHERE root=?', (str(canonical),)).fetchone()
        if not row or con.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
            return
        binding = json.loads(row['body'])
        if binding.get('offline'):
            return
        con.execute('INSERT INTO meta VALUES (?,?)', (marker, str(time.time())))
    try:
        policy = api(binding, 'GET', '/project-binding', timeout=1.5)
        binding.update(enabled=policy['enabled'] is True, policyAt=time.time())
    except (OSError, ValueError, KeyError):
        if time.time() - binding.get('policyAt', 0) <= 86400:
            return
        binding['enabled'] = False
    with database() as con:
        # Do not overwrite a concurrent, intentional relink.
        con.execute('UPDATE bindings SET body=? WHERE root=? AND body=?',
                    (json.dumps(binding), str(canonical), row['body']))


def link(args):
    root, canonical = repo_roots(args.cwd)
    path = root / '.projectflow/project.json'
    old = json.loads(path.read_text()) if path.exists() else {}
    offline = getattr(args, 'offline', False)
    ids = {k: v for k, v in (('tenantId', args.tenant_id), ('defaultProjectId', args.project_id)) if v}
    if len(ids) == 1 or (not offline and len(ids) < 2):
        raise ValueError('Give both --tenant-id and --project-id (only --offline may omit both).')
    if not all(ID.fullmatch(v) for v in ids.values()):
        raise ValueError('Invalid workspace or project ID.')
    binding = {'version': 1, **ids, 'apiBase': validate_api(args.api_base)}
    if old and any(old.get(k) and old[k] != binding.get(k) for k in ('tenantId', 'defaultProjectId')) and not args.replace:
        raise ValueError('Repository already linked. Use --replace for an intentional project change.')
    if offline:
        # No connector/token on this machine: track locally, upload later via `export` + CSV import.
        # Without IDs it is local-only: no ProjectFlow project needed until the CSV is imported.
        binding.update(offline=True, enabled=True, policyAt=time.time(), label=canonical.name)
        with database() as con:
            con.execute('INSERT OR REPLACE INTO bindings VALUES (?,?)', (str(canonical), json.dumps(binding)))
        shared = {k: v for k, v in old.items() if k not in ('tenantId', 'defaultProjectId')}
        atomic_json(path, {**shared, 'version': 1, **ids})
        print(json.dumps({'offline': True, 'localOnly': not ids, 'enabled': True, 'path': str(path),
                          'next': 'Track as usual; run `export` and import the CSV in ProjectFlow.'}))
        return
    token = token_for(binding, explicit=True)
    result = api(binding, 'GET', '/project-binding', token=token)
    verified = result['binding']
    if any(verified[k] != binding[k] for k in ('tenantId', 'defaultProjectId')):
        raise ValueError('Server returned a different workspace/project.')
    binding['enabled'] = result['enabled']
    binding['policyAt'] = time.time()
    if args.save_token:
        atomic_json(data_dir() / 'credentials' / (profile_key(binding) + '.json'), {'token': token})
    with database() as con:
        con.execute('INSERT OR REPLACE INTO bindings VALUES (?,?)', (str(canonical), json.dumps(binding)))
    # Never persist auth, local machine paths or API overrides in shared project metadata.
    atomic_json(path, {**old, **verified})
    print(json.dumps({'project': result['title'], 'enabled': binding['enabled'], 'path': str(path),
                      'credentialSaved': bool(args.save_token)}))


def transcript_for(event, agent):
    path = event.get('transcript_path')
    if not path and agent == 'codex' and ID.fullmatch(str(event.get('session_id') or '')):
        home = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
        found = sorted(glob.glob(os.path.join(home, 'sessions', '*', '*', '*', f"rollout-*{event['session_id']}.jsonl")))
        path = found[-1] if found else None
    return path if path and os.path.isfile(path) else None


def jsonl_from(path, offset=0):
    try:
        with open(path, 'rb') as handle:
            handle.seek(offset)
            for line in handle:
                with contextlib.suppress(ValueError):
                    yield json.loads(line)
    except OSError:
        return


def token_usage(agent, path, offset, started):
    """Tokens spent since the prompt started: usage numbers only, never message contents."""
    totals, by_model = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}, {}
    if agent == 'claude':
        # Streamed replies repeat one message ID per content block; the last line carries the final usage.
        messages = {}
        subagents = glob.glob(os.path.join(path[:-len('.jsonl')], 'subagents', '*.jsonl')) if path.endswith('.jsonl') else []
        for file, start in [(path, offset)] + [(f, 0) for f in subagents]:
            for line in jsonl_from(file, start):
                message = line.get('message') if line.get('type') == 'assistant' else None
                usage = message.get('usage') if isinstance(message, dict) else None
                if not isinstance(usage, dict) or str(line.get('timestamp', '')) < started:
                    continue
                messages[(file, message.get('id') or line.get('uuid'))] = (message.get('model'), usage)
        for model, usage in messages.values():
            counts = {'input': usage.get('input_tokens'), 'output': usage.get('output_tokens'),
                      'cacheRead': usage.get('cache_read_input_tokens'), 'cacheWrite': usage.get('cache_creation_input_tokens')}
            add(totals, by_model, model, counts)
    else:
        # Codex repeats token_count events (rate-limit updates); count a request only when the running total moved.
        model, previous = None, None
        for line in jsonl_from(path, offset):
            payload = line.get('payload') or {}
            if line.get('type') == 'turn_context':
                model = payload.get('model') or model
            if payload.get('type') != 'token_count' or not isinstance(payload.get('info'), dict):
                continue
            total, last = payload['info'].get('total_token_usage'), payload['info'].get('last_token_usage')
            if not isinstance(last, dict) or total == previous:
                continue
            previous = total
            cached = last.get('cached_input_tokens') or 0
            add(totals, by_model, model, {'input': (last.get('input_tokens') or 0) - cached, 'output': last.get('output_tokens'),
                                          'cacheRead': cached, 'cacheWrite': last.get('cache_write_input_tokens')})
    if not any(totals.values()):
        return None
    main = max(by_model, key=by_model.get, default=None)
    return {**totals, **({'model': main} if main and re.fullmatch(r'[a-zA-Z0-9_.:@/-]{1,80}', main) else {})}


def add(totals, by_model, model, counts):
    for key, value in counts.items():
        if isinstance(value, int) and value > 0:
            totals[key] = min(1_000_000_000, totals[key] + value)
    if isinstance(model, str) and model != '<synthetic>':
        by_model[model] = by_model.get(model, 0) + (counts.get('output') or 0) + 1


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def boot_id():
    # Stable across hook processes; detects reboot. monotonic excludes suspension on macOS.
    return str(round((time.time() - time.monotonic()) / 10))


def record(event, agent='codex', clock=None, context=None):
    name = event.get('hook_event_name')
    if name not in EVENTS:
        return False
    sid, turn = event.get('session_id'), event.get('turn_id')
    if not isinstance(sid, str) or not sid:
        return False
    # Codex has a native turn ID. Claude falls back to one persisted run per session.
    if agent == 'codex' and name != 'SessionEnd' and not turn:
        return False
    key = event_key(event, agent)
    root, canonical = repo_roots(event.get('cwd') or os.getcwd())
    now, mono = clock or (time.time(), time.monotonic())
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if name == 'UserPromptSubmit':
            row = con.execute('SELECT body FROM bindings WHERE root=?', (str(canonical),)).fetchone()
            if not row:
                return False
            binding = json.loads(row['body'])
            # Fail closed on unexpected repository relinking (new clones also need explicit link).
            path = root / '.projectflow/project.json'
            if not path.exists():
                path = canonical / '.projectflow/project.json'
            if not path.exists():
                return False
            shared = json.loads(path.read_text())
            if any(shared.get(k) != binding.get(k) for k in ('tenantId', 'defaultProjectId')):
                return False
            # Linked means tracked: upload depends on policy/credential later, local recording never does.
            previous = con.execute('SELECT state,body FROM runs WHERE key=?', (key,)).fetchone()
            if previous:
                if agent == 'codex':
                    return False
                # Claude can submit a new prompt after Escape without a Stop event.
                # Keep the abandoned interval as incomplete; never extend it across prompts.
                if previous['state'] == 'running':
                    old_id = json.loads(previous['body'])['entryId']
                    con.execute("UPDATE runs SET key=?,state='incomplete' WHERE key=?", (key + ':' + old_id, key))
            device = con.execute("SELECT value FROM meta WHERE key='device'").fetchone()
            device_id = device['value'] if device else uuid.uuid4().hex
            con.execute("INSERT OR IGNORE INTO meta VALUES ('device',?)", (device_id,))
            body = {'entryId': uuid.uuid4().hex, 'deviceId': device_id, 'runId': str(turn or uuid.uuid4().hex),
                    'sessionId': sid, 'agent': agent, 'startedAt': iso(now), 'captureMethod': 'hooks',
                    'quality': 'elapsed'}
            transcript = transcript_for(event, agent)
            if transcript:
                # Local-only marker (stripped before upload): tokens are counted from here on at Stop.
                body['_transcript'] = [transcript, os.path.getsize(transcript)]
            con.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)',
                        (key, json.dumps(binding), json.dumps(body), now, mono, boot_id(), 'running'))
            if context is not None:
                context['hookSpecificOutput'] = {'hookEventName': name, 'additionalContext': SUMMARY_CONTEXT}
            return False
        rows = con.execute('SELECT * FROM runs WHERE state=?', ('running',)).fetchall()
        queued = False
        for row in rows:
            body = json.loads(row['body'])
            if row['key'] != key and not (name == 'SessionEnd' and body['sessionId'] == sid and body['agent'] == agent):
                continue
            if name == 'SessionEnd':
                # Unknown work end: retain evidence locally, never invent elapsed hours.
                con.execute("UPDATE runs SET state='incomplete' WHERE key=?", (row['key'],))
                continue
            wall, elapsed = now - row['started'], mono - row['monotonic']
            uncertain = elapsed < 0 or wall < 0 or abs(wall - elapsed) > 2 or elapsed > 86400
            body.update(endedAt=iso(max(now, row['started'])), durationMs=max(0, min(86400000, round(elapsed * 1000))),
                        quality='clock-anomaly' if uncertain else 'elapsed',
                        outcome='interrupted' if name == 'Interrupt' else 'completed')
            transcript = body.pop('_transcript', None)
            if transcript:
                with contextlib.suppress(Exception):  # token counts are a bonus; never lose the interval over them
                    tokens = token_usage(agent, transcript[0], transcript[1], body['startedAt'])
                    if tokens:
                        body['tokens'] = tokens
            summary = activity_summary(event.get('last_assistant_message')) if name == 'Stop' else None
            if summary:
                body['description'] = summary
            missing = name == 'Stop' and not summary
            # Clock/reboot/sleep anomalies stay local for review; no fictional time reaches the server.
            if uncertain:
                con.execute("UPDATE runs SET state='clock-anomaly' WHERE key=?", (row['key'],))
            else:
                con.execute('INSERT OR IGNORE INTO outbox(id,binding,body,state,error) VALUES (?,?,?,?,?)',
                            (body['entryId'], row['binding'], json.dumps(body),
                             'review' if missing else 'pending', 'missing-description' if missing else None))
                con.execute("UPDATE runs SET state='closed' WHERE key=?", (row['key'],))
                queued = queued or not missing
        return queued


def same_target(a, b):
    return all(a.get(k) == b.get(k) for k in ('apiBase', 'tenantId', 'defaultProjectId'))


def auto_upload(snapshot, bindings, need_token=False):
    """Online, enabled link for the entry's project (current state, not the snapshot at recording time)."""
    current = next((b for b in bindings if same_target(b, snapshot)), None)
    return bool(current and not current.get('offline') and current.get('enabled') and current.get('tenantId')
                and (not need_token or token_for(current)))


def sync(force=False):
    # A short lease prevents concurrent Stop hooks from issuing redundant requests.
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        lease = con.execute("SELECT value FROM meta WHERE key='syncLease'").fetchone()
        if lease and float(lease['value']) > time.time():
            return
        con.execute("INSERT OR REPLACE INTO meta VALUES ('syncLease',?)", (str(time.time()+60),))
    try:
        with database() as con:
            rows = con.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY rowid LIMIT 100",
                               (float('inf') if force else time.time(),)).fetchall()
            bindings = [json.loads(r['body']) for r in con.execute('SELECT body FROM bindings')]
        groups = {}
        for row in rows:
            if row['error'] == 'missing-description':
                with database() as con:
                    con.execute("UPDATE outbox SET state='review' WHERE id=?", (row['id'],))
                continue
            b = json.loads(row['binding'])
            if not auto_upload(b, bindings):
                continue  # offline, disabled or unlinked: stays pending for a later upload or `export`
            k = (b['apiBase'], b['tenantId'], b['defaultProjectId'])
            groups.setdefault(k, (b, []))[1].append(row)
        for binding, batch in groups.values():
            try:
                result = api(binding, 'POST', '/agent-time', {'entries': [json.loads(r['body']) for r in batch]})
                receipts = {r['entryId']: r['status'] for r in result['results']}
                with database() as con:
                    for row in batch:
                        state = receipts.get(row['id'])
                        if state in ('accepted', 'duplicate'):
                            con.execute("UPDATE outbox SET state='sent',error=NULL WHERE id=?", (row['id'],))
                        elif state == 'conflict':
                            con.execute("UPDATE outbox SET state='review',error='conflict' WHERE id=?", (row['id'],))
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('lastSync',?)", (iso(time.time()),))
            except (OSError, ValueError, KeyError) as exc:
                code = exc.code if isinstance(exc, error.HTTPError) else 0
                if code == 409:
                    with database() as con:
                        for row in con.execute('SELECT root,body FROM bindings').fetchall():
                            current = json.loads(row['body'])
                            if all(current.get(k) == binding.get(k) for k in ('apiBase', 'tenantId', 'defaultProjectId')):
                                current['enabled'] = False
                                con.execute('UPDATE bindings SET body=? WHERE root=?', (json.dumps(current), row['root']))
                # No response body, token or prompt in diagnostics.
                with database() as con:
                    for row in batch:
                        delay = min(3600, 2 ** min(row['attempts']+2, 11)) + random.random()*3
                        state = 'review' if code in (400, 401, 403, 404) or isinstance(exc, ValueError) else 'pending'
                        con.execute('UPDATE outbox SET attempts=attempts+1,state=?,next_try=?,error=? WHERE id=?',
                                    (state, time.time()+delay, str(code or type(exc).__name__), row['id']))
    finally:
        with database() as con:
            con.execute("DELETE FROM meta WHERE key='syncLease'")


EXPORT_COLUMNS = ('id', 'start', 'end', 'agent', 'description', 'task',
                  'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'model')


def export(out_dir, include_exported=False, project_id=None):
    """Intervals that cannot go up automatically → one CSV per project for ProjectFlow's import (id keeps re-imports idempotent)."""
    states = ('pending', 'review', 'exported') if include_exported else ('pending', 'review')
    with database() as con:
        rows = con.execute(f"SELECT id,binding,body,state,error FROM outbox WHERE state IN ({','.join('?' * len(states))}) ORDER BY rowid",
                           states).fetchall()
    with database() as con:
        bindings = [json.loads(r['body']) for r in con.execute('SELECT body FROM bindings')]
    groups = {}
    for row in rows:
        binding = json.loads(row['binding'])
        # Either the automatic upload or the file, never both: entries that sync will send are left to sync.
        if row['state'] == 'pending' and auto_upload(binding, bindings, need_token=True):
            continue
        # Server conflicts mean a different entry with this ID exists there; never import those twice.
        if row['state'] == 'review' and row['error'] == 'conflict':
            continue
        if project_id and binding.get('defaultProjectId') != project_id:
            continue
        groups.setdefault((binding.get('tenantId'), binding.get('defaultProjectId') or binding.get('label') or 'local'), []).append(row)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M')
    files = []
    for (tenant, project), batch in groups.items():
        path = out_dir / f"agent-time-{re.sub(r'[^A-Za-z0-9_.-]+', '-', project).strip('-') or 'local'}-{stamp}.csv"
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            for row in batch:
                body = json.loads(row['body'])
                description = body.get('description') or ''
                # Spreadsheet formula injection if someone opens the file before importing it.
                if description[:1] in ('=', '+', '-', '@'):
                    description = "'" + description
                tokens = body.get('tokens') or {}
                writer.writerow((body['entryId'], body['startedAt'], body['endedAt'], body['agent'], description, body.get('taskId') or '',
                                 *(tokens.get(k, '') for k in ('input', 'output', 'cacheRead', 'cacheWrite', 'model'))))
        with database() as con:
            con.executemany("UPDATE outbox SET state='exported',error=NULL WHERE id=?", [(r['id'],) for r in batch])
        files.append({'path': str(path), 'projectId': project if tenant else None, 'tenantId': tenant, 'entries': len(batch),
                      'hours': round(sum(json.loads(r['body'])['durationMs'] for r in batch) / 3_600_000, 2)})
    return files


def install(args):
    target = data_dir() / 'agent_time.py'
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, target)
        target.chmod(0o700)
    folder = Path.home() / ('.codex' if args.client == 'codex' else '.claude')
    path = folder / ('hooks.json' if args.client == 'codex' else 'settings.json')
    config = json.loads(path.read_text()) if path.exists() else {}
    hooks = config.setdefault('hooks', {})
    events = EVENTS if args.client == 'codex' else ('UserPromptSubmit', 'Stop', 'SessionEnd')
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(target))} hook --agent {args.client}'
    for event in events:
        groups = hooks.setdefault(event, [])
        if not any(h.get('command') == command for g in groups for h in g.get('hooks', [])):
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]})
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + '.projectflow-backup-' + str(time.time_ns())))
    atomic_json(path, config)
    next_step = ('Review and trust ProjectFlow hooks in Codex /hooks; restart existing clients.'
                 if args.client == 'codex' else 'Review ProjectFlow hooks in Claude Code /hooks; restart existing clients.')
    print(json.dumps({'path': str(path), 'next': next_step}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('link')
    p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--project-id')
    p.add_argument('--tenant-id')
    p.add_argument('--api-base', default=DEFAULT_API)
    p.add_argument('--replace', action='store_true')
    p.add_argument('--save-token', action='store_true')
    p.add_argument('--offline', action='store_true', help='No token or connector: track locally and export a CSV for manual import.')
    p = sub.add_parser('export', help='Write offline intervals as CSV for ProjectFlow → project settings → agent time → import.')
    p.add_argument('--out', default=os.getcwd())
    p.add_argument('--project-id')
    p.add_argument('--all', action='store_true', help='Include entries exported before (safe: re-imports add nothing).')
    p = sub.add_parser('install')
    p.add_argument('--client', choices=['codex', 'claude'], default='codex')
    p = sub.add_parser('hook')
    p.add_argument('--agent', choices=['codex', 'claude'], default='codex')
    p = sub.add_parser('sync')
    p.add_argument('--retry-review', action='store_true', help='Explicitly retry held entries after fixing auth/policy/conflict.')
    p = sub.add_parser('describe')
    p.add_argument('--entry-id', required=True)
    p.add_argument('--description', required=True)
    sub.add_parser('status')
    args = parser.parse_args()
    if args.command == 'hook':
        output = {}
        try:
            clock = (time.time(), time.monotonic())
            event = json.load(sys.stdin)
            refresh_session_policy(event)
            output, queued = process_event(event, args.agent, clock=clock)
            if queued:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'sync'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        except Exception as exc:
            # Fail open on infrastructure errors; never block a user's prompt.
            with contextlib.suppress(Exception):
                with database() as con:
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('lastError',?)", (type(exc).__name__,))
        print(json.dumps(output))
    elif args.command == 'link':
        link(args)
    elif args.command == 'install':
        install(args)
    elif args.command == 'export':
        files = export(args.out, include_exported=args.all, project_id=args.project_id)
        print(json.dumps({'files': files, 'next': 'Import each file in ProjectFlow: project settings → Agent time → Import past times.'
                          if files else 'Nothing new to export.'}))
    elif args.command == 'describe':
        changed = describe(args.entry_id, args.description)
        if changed:
            sync(force=True)
        print(json.dumps({'updated': changed}))
    elif args.command == 'sync':
        if args.retry_review:
            with database() as con:
                con.execute("UPDATE outbox SET state='pending',next_try=0 WHERE state='review' AND (error IS NULL OR error!='missing-description')")
        sync(force=args.retry_review)
    else:
        with database() as con:
            print(json.dumps({'queue': {r[0]: r[1] for r in con.execute('SELECT state,count(*) FROM outbox GROUP BY state')},
                              'runs': {r[0]: r[1] for r in con.execute('SELECT state,count(*) FROM runs GROUP BY state')},
                              'lastSync': dict(con.execute('SELECT key,value FROM meta')).get('lastSync'),
                              'missingDescriptions': [r[0] for r in con.execute("SELECT id FROM outbox WHERE state='review' AND error='missing-description'")],
                              'lastError': dict(con.execute('SELECT key,value FROM meta')).get('lastError')}))


if __name__ == '__main__':
    main()
