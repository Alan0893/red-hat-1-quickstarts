"""Exercise the demo data script against local HTTP service doubles."""

import importlib.util
import io
import json
import base64
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'tenant/bootstrap/files/seed-demo.py'


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.mail = [{'ID': 'unrelated', 'Subject': 'Other workshop email'}]
        self.posts = {'other': {'id': 'other', 'message': 'Other workshop', 'props': {}}}
        self.events = {}
        self.created = {}
        self.requests = []
        self.fail_path = None
        self.calendar_auth = []
        self.login_password = None
        self.team_members = None
        self.missing_tenant = False
        self.missing_tenant_status = 401
        self.login_failure_code = None
        self.lookup_failure_code = None
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                size = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(size)
                body = json.loads(raw) if raw and self.headers.get('Content-Type') == 'application/json' else raw
                path = self.path.split('?')[0]
                state.requests.append((self.command, path))
                code, result = 200, {}
                headers = {}
                if state.fail_path and path.endswith(state.fail_path):
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"sensitive upstream error admin-secret")
                    return
                if path.startswith('/identity'):
                    result = {'access_token': 'verified-keycloak-identity'}
                elif path.startswith('/mail'):
                    if self.command == 'GET' and '/message/' in path:
                        result = next(m for m in state.mail if m['ID'] == path.rsplit('/', 1)[-1])
                    elif self.command == 'GET':
                        result = {'messages': state.mail, 'total': len(state.mail)}
                    elif self.command == 'POST':
                        result = dict(body, ID=str(len(state.mail)))
                        state.mail.append(result)
                    elif self.command == 'DELETE':
                        state.mail[:] = [m for m in state.mail if m['ID'] not in body['IDs']]
                        result = 'ok'
                        headers['Content-Type'] = 'text/plain'
                elif path.startswith('/chat'):
                    if path.endswith('/users/login'):
                        if body['login_id'] != 'saw-lab-admin' and state.login_failure_code:
                            code = state.login_failure_code
                        elif body['login_id'] != 'saw-lab-admin' and state.missing_tenant and 'user' not in state.created:
                            code = state.missing_tenant_status
                        elif state.login_password is not None and body['password'] != state.login_password:
                            code = 401
                        else:
                            headers['Token'] = 'demo-session-secret'
                        result = {'id': 'demo-user'}
                    elif self.command == 'GET' and path.endswith('/users/me'):
                        result = {'id': 'demo-admin'}
                    elif self.command == 'GET' and path.endswith('/teams/demo-team/members'):
                        result = state.team_members if state.team_members is not None else [{'user_id':'demo-admin'}, {'user_id':'demo-user'}]
                    elif self.command == 'GET' and '/posts' in path:
                        result = {'posts': state.posts, 'order': list(state.posts)}
                    elif self.command == 'POST' and path.endswith('/posts'):
                        result = dict(body, id=str(len(state.posts)))
                        state.posts[result['id']] = result
                    elif self.command == 'DELETE' and '/posts/' in path:
                        del state.posts[path.rsplit('/', 1)[-1]]
                    elif self.command == 'GET':
                        if '/users/username/' in path:
                            key = 'user'
                        elif '/channels/name/' in path:
                            key = 'channel'
                        elif '/teams/name/' in path:
                            key = 'team'
                        else:
                            key = 'member'
                        if key in state.created:
                            result = state.created[key]
                        else:
                            code = 404
                        if key == 'user' and state.lookup_failure_code:
                            code = state.lookup_failure_code
                    elif self.command == 'POST':
                        key = ('user' if path.endswith('/users') else
                               'team' if path.endswith('/teams') else
                               'channel' if path.endswith('/channels') else 'member')
                        result = {'id': 'demo-' + key, **body}
                        state.created[key] = result
                elif path.startswith('/cal'):
                    state.calendar_auth.append(self.headers.get('Authorization'))
                    if self.command == 'PROPFIND':
                        key = 'calendar-parent' if path == '/cal/demo/' else 'calendar'
                        code = 207 if state.created.get(key) else 404
                    elif self.command == 'MKCOL':
                        state.created['calendar-parent'] = True
                        code = 201
                    elif self.command == 'MKCALENDAR':
                        if state.created.get('calendar-parent'):
                            state.created['calendar'] = True
                            code = 201
                        else:
                            code = 409
                    elif self.command == 'PUT':
                        state.events[path] = raw.decode()
                        code = 201
                    elif self.command == 'DELETE':
                        state.events.pop(path, None)
                    elif self.command == 'GET':
                        if path in state.events:
                            result = state.events[path]
                        else:
                            code = 404
                data = result.encode() if isinstance(result, str) else json.dumps(result).encode()
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PROPFIND = do_MKCOL = do_MKCALENDAR = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp.name) / 'state.json'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_script(self, action, expected_code=0, date='2026-11-12', extra_args=(), **extra_env):
        url = f'http://127.0.0.1:{self.server.server_port}'
        result = subprocess.run([
            sys.executable, str(SCRIPT), action, '--demo-id', 'test',
            '--mailpit-url', url + '/mail', '--mattermost-url', url + '/chat',
            '--radicale-url', url + '/cal', '--state', str(self.state_file),
            *(['--date', date] if date else []), *extra_args,
        ], env={**os.environ, 'MATTERMOST_ADMIN_TOKEN': 'admin-secret',
                'SAW_DEMO_PASSWORD': 'ExamplePassword123!', **extra_env},
            text=True, capture_output=True)
        self.assertNotIn('admin-secret', result.stdout + result.stderr)
        self.assertNotIn('demo-session-secret', result.stdout + result.stderr)
        self.assertNotIn('calendar-secret', result.stdout + result.stderr)
        self.assertNotIn('ExamplePassword123!', result.stdout + result.stderr)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        return result

    def test_provisioned_identity_owns_private_team_recipient_and_calendar(self):
        self.run_script('seed', extra_args=('--username', 'user-test'))
        self.assertEqual(self.created['user']['username'], 'user-test')
        self.assertEqual(self.created['team']['name'], 'user-test')
        self.assertEqual(self.created['team']['type'], 'I')
        self.assertEqual(self.mail[1]['To'][0]['Email'], 'user-test@example.test')
        self.assertEqual(set(self.events), {f'/cal/user-test/test/event-{i}.ics' for i in range(3)})
        self.run_script('verify', extra_args=('--username', 'user-test'))
        self.run_script('reset', extra_args=('--username', 'user-test'))
        self.assertEqual(self.events, {})

    def test_existing_identity_with_wrong_password_is_not_modified(self):
        self.created['user'] = {'id': 'existing-user', 'username': 'user-test'}
        self.login_password = 'ExistingPassword123!'
        result = self.run_script('seed', expected_code=1, extra_args=('--username', 'user-test'))
        self.assertIn('HTTP 401', result.stderr)
        self.assertEqual(self.created, {'user': {'id': 'existing-user', 'username': 'user-test'}})
        self.assertEqual(self.mail, [{'ID': 'unrelated', 'Subject': 'Other workshop email'}])

    def test_existing_public_or_foreign_team_is_rejected_before_data_writes(self):
        self.run_script('seed', extra_args=('--username', 'user-test'))
        self.created['team']['type'] = 'O'
        self.requests.clear()
        self.run_script('seed', expected_code=1, extra_args=('--username', 'user-test'))
        self.run_script('seed', expected_code=1, extra_args=('--username', 'user-test'), MATTERMOST_ADMIN_TOKEN='', SAW_DEMO_PASSWORD='')
        self.assertFalse(any(method in ('POST','PUT','DELETE') and not path.endswith('/users/login') for method,path in self.requests))
        self.created['team']['type'] = 'I'
        self.team_members = [{'user_id':'other-tenant'}]
        self.requests.clear()
        self.run_script('seed', expected_code=1, extra_args=('--username', 'user-test'))
        self.run_script('seed', expected_code=1, extra_args=('--username', 'user-test'), MATTERMOST_ADMIN_TOKEN='', SAW_DEMO_PASSWORD='')
        self.assertFalse(any(method in ('POST','PUT','DELETE') and not path.endswith('/users/login') for method,path in self.requests))

    def test_existing_private_state_can_reseed_without_admin_credentials(self):
        self.run_script('seed')
        before = json.loads(self.state_file.read_text())
        self.run_script('reset')
        self.run_script('seed', date=None, MATTERMOST_ADMIN_TOKEN='', SAW_DEMO_PASSWORD='')
        self.run_script('verify')
        self.assertEqual(json.loads(self.state_file.read_text()), before)

    def test_reseed_repairs_owned_fixtures_and_preserves_unrelated_records(self):
        self.run_script('seed')
        self.mail[1]['Text'] = 'stale generated body'
        self.mail.extend(
            [
                {
                    'ID': 'legacy',
                    'Subject': '[SAW:test] Q4 platform planning',
                    'Text': 'old scenario',
                    'From': {'Email': 'platform-demo@example.test'},
                    'To': [{'Email': 'saw-test@example.test'}],
                },
                {'ID': 'unrelated-owned-prefix', 'Subject': '[SAW:test] My unrelated notes', 'Text': 'preserve'},
            ]
        )
        self.posts['stale'] = {'id': 'stale', 'message': 'Old generated baseline', 'props': {'rh1_saw_demo': 'test'}}
        self.posts['foreign'] = {'id': 'foreign', 'message': 'Other tenant', 'props': {'rh1_saw_demo': 'other'}}
        challenge = next(p for p in self.posts.values() if p.get('props', {}).get('rh1_saw_challenge'))
        self.posts[challenge['id']]['message'] = 'Old generated challenge'
        self.run_script('seed')
        self.run_script('verify')
        self.assertFalse(any(m['ID'] == 'legacy' or m.get('Text') == 'stale generated body' for m in self.mail))
        self.assertNotIn('stale', self.posts)
        self.assertEqual(self.posts['other']['message'], 'Other workshop')
        self.assertEqual(self.posts['foreign']['message'], 'Other tenant')
        self.assertEqual(sum(bool(p.get('props', {}).get('rh1_saw_challenge')) for p in self.posts.values()), 1)
        self.run_script('reset')
        self.assertEqual({m['ID'] for m in self.mail}, {'unrelated', 'unrelated-owned-prefix'})
        self.assertEqual(set(self.posts), {'other', 'foreign'})

    def test_reseed_and_reset_preserve_known_subjects_outside_generated_mail_ownership(self):
        self.run_script('seed')
        self.mail.extend([
            {'ID':'foreign-recipient','Subject':'[SAW:test] Q4 platform planning','Text':'preserve recipient',
             'From':{'Address':'platform-demo@example.test'},'To':[{'Address':'another-user@example.test'}]},
            {'ID':'foreign-sender','Subject':'[SAW:test] Q4 platform planning','Text':'preserve sender',
             'From':{'Address':'another-sender@example.test'},'To':[{'Address':'saw-test@example.test'}]},
        ])
        self.run_script('seed')
        self.run_script('reset')
        self.assertEqual({m['ID'] for m in self.mail}, {'unrelated','foreign-recipient','foreign-sender'})

    def test_repeatable_seed_verify_and_scoped_reset(self):
        self.run_script('seed')
        first = len(self.mail), len(self.posts), len(self.events)
        self.assertEqual(first, (7, 16, 3))
        self.run_script('seed')
        self.assertEqual((len(self.mail), len(self.posts), len(self.events)), first)
        self.run_script('verify')
        challenge_post = next(p for p in self.posts.values() if p.get('props', {}).get('rh1_saw_challenge'))
        challenge = challenge_post['message']
        self.assertIn('https://compliance-sync.example.invalid/collect', challenge)
        self.assertIn('exec', challenge)
        self.assertIn('curl', challenge)
        self.assertEqual(challenge_post['props']['rh1_saw_demo'], 'test')
        self.events['/cal/demo/test/unrelated.ics'] = 'Unrelated calendar event'
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved['mattermost_token'], 'demo-session-secret')
        self.run_script('reset')
        self.assertEqual([m['ID'] for m in self.mail], ['unrelated'])
        self.assertEqual(list(self.posts), ['other'])
        self.assertEqual(self.events, {'/cal/demo/test/unrelated.ics': 'Unrelated calendar event'})

    def test_verify_detects_missing_or_changed_source_data(self):
        self.run_script('seed', date='2024-01-15')
        self.run_script('verify', date=None)
        snapshot = json.dumps([self.mail, self.posts, self.events])
        for source in ('email-body', 'missing-email', 'chat', 'challenge', 'calendar'):
            with self.subTest(source=source):
                self.mail, self.posts, self.events = json.loads(snapshot)
                if source == 'email-body':
                    self.mail[1]['Text'] = 'tampered email content'
                elif source == 'missing-email':
                    self.mail.pop()
                elif source == 'chat':
                    del self.posts['1']
                elif source == 'challenge':
                    challenge = next(p for p in self.posts.values() if p.get('props', {}).get('rh1_saw_challenge'))
                    del self.posts[challenge['id']]
                else:
                    path = '/cal/demo/test/event-1.ics'
                    self.events[path] = self.events[path].replace('T140000Z', 'T090000Z')
                self.run_script('verify', expected_code=1, date=None)
        self.mail, self.posts, self.events = json.loads(snapshot)
        self.run_script('verify', date=None)

    def test_partial_failure_is_recoverable_without_duplicates_or_secret_logging(self):
        for fail_path in ('/api/v1/send', '/event-1.ics'):
            with self.subTest(endpoint=fail_path):
                self.fail_path = fail_path
                failed = self.run_script('seed', expected_code=1)
                self.assertNotIn('sensitive upstream error', failed.stderr)
                self.assertTrue(self.state_file.exists())
                self.fail_path = None
                self.run_script('seed')
                self.assertEqual((len(self.mail), len(self.posts), len(self.events)), (7, 16, 3))
                self.run_script('verify')
                self.run_script('reset')

    def test_insecure_or_symlink_state_is_rejected_before_mutation(self):
        self.run_script('seed')
        self.state_file.chmod(0o644)
        self.requests.clear()
        self.run_script('verify', expected_code=1)
        self.assertEqual(self.requests, [])
        target = self.state_file.with_name('target.json')
        self.state_file.rename(target)
        self.state_file.symlink_to(target)
        self.run_script('seed', expected_code=1)
        self.assertEqual(self.requests, [])

    def test_calendar_basic_auth_and_reject_partial_credentials_before_mutation(self):
        self.run_script('seed', expected_code=1, RADICALE_USERNAME='demo', RADICALE_PASSWORD='')
        self.assertEqual(self.requests, [])
        self.run_script('seed', RADICALE_USERNAME='demo', RADICALE_PASSWORD='calendar-secret')
        expected = 'Basic ' + base64.b64encode(b'demo:calendar-secret').decode()
        self.assertTrue(self.calendar_auth)
        self.assertEqual(set(self.calendar_auth), {expected})

    def test_missing_demo_password_fails_before_mutation(self):
        self.run_script('seed', expected_code=1, SAW_DEMO_PASSWORD='')
        self.assertEqual(self.requests, [])

    def test_reset_is_idempotent(self):
        self.run_script('seed')
        self.run_script('reset')
        self.run_script('reset')
        self.assertEqual([m['ID'] for m in self.mail], ['unrelated'])
        self.assertEqual(list(self.posts), ['other'])

    def test_seed_creates_missing_calendar_parent_once_and_retains_collections(self):
        self.run_script('seed')
        self.run_script('seed')
        parent_create = ('MKCOL', '/cal/demo/')
        calendar_create = ('MKCALENDAR', '/cal/demo/test/')
        self.assertEqual(self.requests.count(parent_create), 1)
        self.assertEqual(self.requests.count(calendar_create), 1)
        self.assertLess(self.requests.index(parent_create), self.requests.index(calendar_create))
        self.run_script('reset')
        self.assertTrue(self.created['calendar-parent'])
        self.assertTrue(self.created['calendar'])
        self.assertFalse(any(method == 'DELETE' and path.endswith('/') for method, path in self.requests))

    def test_missing_admin_token_fails_before_any_mutation(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'seed'],
                                env={**os.environ, 'MATTERMOST_ADMIN_TOKEN': ''},
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests, [])

    def configure_tenant(self):
        spec = importlib.util.spec_from_file_location('recover_demo', SCRIPT.with_name('saw-demo-provision.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = module.seed.Client
        base = f'http://127.0.0.1:{self.server.server_port}'
        def remote(url, *args, **kwargs):
            service = ('identity' if url.startswith('https://identity.test') else
                       'chat' if 'mattermost.' in url else 'mail' if 'mailpit.' in url else 'cal')
            return client(base + '/' + service, *args, **kwargs)
        written = {}
        with patch.object(module.seed, 'Client', remote), patch.dict(os.environ, {}, clear=False):
            module.configure('user-test', 'ExamplePassword123!', 'https://identity.test',
                             {'username': 'saw-lab-admin', 'password': 'ExamplePassword123!'},
                             self.state_file, lambda source, data: written.update({source: data}))
        return written

    def test_retained_state_recovers_only_a_missing_mattermost_account(self):
        self.configure_tenant()
        for status in (401, 404):
            with self.subTest(login=status):
                # Only the chat database was lost; email/calendar and private state survived.
                saved = json.loads(self.state_file.read_text())
                saved.update(user_id='erased-user', channel_id='erased-channel')
                self.state_file.write_text(json.dumps(saved))
                self.created.clear()
                self.posts = {'other': {'id': 'other', 'message': 'Other workshop', 'props': {}}}
                self.missing_tenant, self.missing_tenant_status = True, status
                written = self.configure_tenant()
                self.assertEqual(self.created['user']['username'], 'user-test')
                self.assertEqual(self.created['team']['type'], 'I')
                self.assertEqual((len(self.mail), len(self.posts), len(self.events)), (7, 16, 3))
                self.assertIn('other', self.posts)
                self.assertEqual(set(written), {'mailpit', 'mattermost', 'calendar'})
                self.assertEqual(json.loads(self.state_file.read_text())['channel_id'], 'demo-channel')

    def test_saved_state_login_failures_do_not_replace_existing_identity(self):
        for login_code, lookup_code in [(401, None), (404, None), (503, None), (401, 403), (401, 503)]:
            with self.subTest(login=login_code, lookup=lookup_code):
                self.login_failure_code = self.lookup_failure_code = None
                self.configure_tenant()
                saved = self.state_file.read_text()
                self.requests.clear()
                self.login_failure_code, self.lookup_failure_code = login_code, lookup_code
                with self.assertRaises(RuntimeError):
                    self.configure_tenant()
                self.assertEqual(self.state_file.read_text(), saved)
                self.assertFalse(any(method in ('POST', 'PUT', 'DELETE') and not path.startswith('/identity/') and not path.endswith('/users/login')
                                     for method, path in self.requests))
                if login_code == 503:
                    self.assertNotIn(('GET', '/chat/api/v4/users/username/user-test'), self.requests)
                else:
                    self.assertIn(('GET', '/chat/api/v4/users/username/user-test'), self.requests)

    def test_missing_account_rebuild_can_retry_after_user_creation(self):
        self.configure_tenant()
        self.created.clear()
        self.posts = {'other': {'id': 'other', 'message': 'Other workshop', 'props': {}}}
        self.missing_tenant = True
        self.fail_path = '/channels/name/ai-platform'
        with self.assertRaises(RuntimeError):
            self.configure_tenant()
        self.assertIn('user', self.created)
        self.assertTrue(json.loads(self.state_file.read_text())['mattermost_rebuild_pending'])
        self.fail_path = None
        self.configure_tenant()
        self.assertNotIn('mattermost_rebuild_pending', json.loads(self.state_file.read_text()))
        self.assertEqual((len(self.mail), len(self.posts), len(self.events)), (7, 16, 3))

    def test_missing_account_rebuild_refuses_a_foreign_surviving_team(self):
        self.configure_tenant()
        del self.created['user']
        self.missing_tenant = True
        self.team_members = [{'user_id': 'another-tenant'}]
        before = json.dumps([self.mail, self.posts, self.events], sort_keys=True)
        self.requests.clear()
        with self.assertRaisesRegex(ValueError, 'ownership or other members'):
            self.configure_tenant()
        self.assertEqual(json.dumps([self.mail, self.posts, self.events], sort_keys=True), before)
        self.assertFalse(any(method in ('PUT', 'DELETE') or
                             (method == 'POST' and path.endswith(('/posts', '/send', '/members')))
                             for method, path in self.requests))


class ProvisionRuntimeTests(unittest.TestCase):
    def setUp(self):
        path = SCRIPT.with_name('saw-demo-provision.py')
        spec = importlib.util.spec_from_file_location('demo_provision', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_keycloak_account_preserves_the_lab_password_whitespace(self):
        password = '  LabPassword123!  '
        created = {}
        def response(request, **kwargs):
            url = request.full_url
            if url.endswith('/token'):
                result = {'access_token': 'administrator-session'}
            elif request.method == 'POST' and url.endswith('/users'):
                created.update(json.loads(request.data))
                result = None
            elif '/users?' in url:
                result = [{'id': 'tenant-user'}] if created else []
            elif '/roles/' in url:
                result = {'id': url.rsplit('/', 1)[-1]}
            else:
                result = None
            return io.BytesIO(json.dumps(result).encode() if result is not None else b'')
        def read(path, *args, **kwargs):
            return password if str(path) == '/input/password' else 'private-admin'
        with patch.dict(os.environ, {'SAW_USER': 'user-test'}), patch.object(Path, 'read_text', read), patch('urllib.request.urlopen', response):
            runpy.run_path(str(SCRIPT.with_name('saw-create-user.py')))
        self.assertEqual(created['credentials'][0]['value'], password)

    def test_identity_failure_causes_no_seed_or_vault_mutation(self):
        module = self.module
        class Client:
            def __init__(self, *args): pass
            def request(self, *args): raise RuntimeError('HTTP 401')
        with tempfile.TemporaryDirectory() as root, patch.object(module.seed, 'Client', Client), patch.object(module.seed, 'run') as seed:
            with self.assertRaisesRegex(RuntimeError, '401'):
                module.configure('user-test', 'wrong', 'https://identity.test/realms/openshell', {}, Path(root)/'state', lambda *args: self.fail('unexpected Vault write'))
            seed.assert_not_called()

    def test_valid_saved_session_does_not_hide_a_mismatched_lab_password(self):
        module = self.module
        class Client:
            def __init__(self, url, token=''): pass
            def request(self, method, path, *args):
                if path == '/protocol/openid-connect/token':
                    return b'{"access_token":"identity-token"}', {}
                if path == '/api/v4/users/login':
                    raise RuntimeError('Mattermost password does not match the lab password')
                raise AssertionError(path)
            def json(self, method, path):
                return {'username': 'user-test'}
        with tempfile.TemporaryDirectory() as root, patch.object(module.seed, 'Client', Client), patch.object(module.seed, 'run') as seed:
            path = Path(root) / 'state'
            saved = json.dumps({'username': 'user-test', 'demo_id': 'user-test', 'mattermost_token': 'existing-session'})
            path.write_text(saved)
            with self.assertRaisesRegex(RuntimeError, 'password'):
                module.configure('user-test', 'new-lab-password', 'https://identity.test', {}, path,
                                 lambda *args: self.fail('unexpected Vault write'))
            self.assertEqual(path.read_text(), saved)
            seed.assert_not_called()

    def test_reseed_verifies_lab_password_and_writes_only_own_provider_credentials(self):
        module = self.module
        calls, written = [], {}
        class Client:
            def __init__(self, url, token=''): self.url, self.token = url, token
            def request(self, method, path, *args):
                calls.append((self.url,path))
                if path == '/protocol/openid-connect/token': return b'{"access_token":"identity-token"}', {}
                if path == '/api/v4/users/login':
                    if args[0] != {'login_id': 'user-test', 'password': 'LabPassword123!'}:
                        raise AssertionError('must verify the participant password, not the admin password')
                    return b'{}', {'Token': 'verified-session'}
                raise AssertionError('unexpected source read')
        with tempfile.TemporaryDirectory() as root, patch.object(module.seed, 'Client', Client), patch.object(module.seed, 'run') as seed, patch.dict(os.environ, {'MATTERMOST_ADMIN_TOKEN':'stale-admin'}):
            path = Path(root)/'state'
            path.write_text(json.dumps({'username':'user-test','demo_id':'user-test','mattermost_token':'existing-session'}))
            result = module.configure('user-test','LabPassword123!','https://identity.test/realms/openshell',{},path, lambda source,data: written.update({source:data}))
            self.assertEqual(result['mattermost_token'],'verified-session')
            self.assertEqual(seed.call_count, 2)
            self.assertNotIn('MATTERMOST_ADMIN_TOKEN', os.environ)
            self.assertEqual(set(written), {'mailpit','calendar','mattermost'})
            self.assertEqual(written['mattermost']['api_key'],'verified-session')
            self.assertTrue(written['mailpit']['api_key'].startswith('Basic '))
            self.assertEqual(len(calls),2)


if __name__ == '__main__':
    unittest.main()
