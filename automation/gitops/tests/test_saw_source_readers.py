import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'tenant/saw/charts/openshell-saw/files/installer/apply_bom.py'
spec = importlib.util.spec_from_file_location('saw_source_installer', PATH)
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


class SourceReaderInstallerTests(unittest.TestCase):
    def test_demo_tool_names_are_compatible_with_the_scout_function_parser(self):
        info = installer.describe_harness_tree(self.bundle(), inline=True)
        self.assertTrue(info['mcpServers'])
        self.assertTrue(all(name.isidentifier() for name in info['mcpServers']),
                        'MCP server prefixes must be compatible with the Scout function parser')

    def bundle(self):
        values = {'profiles': ['demo-assistant'], 'harnessEnabled': True,
                  'demoAssistant': {'enabled': True, 'clusterDomain': 'test.example',
                                    'demoId': 'rh1', 'tenantUser': 'rh1'}}
        output = subprocess.check_output(['helm', 'template', 'test', str(ROOT / 'tenant/saw/charts/saw-bom'),
                                          '-f', '-'], input=yaml.safe_dump(values), text=True)
        data = yaml.safe_load(output)['data']
        bundle = installer.parse_harness_files({k: base64.b64decode(v) for k, v in data.items()
                                               if k.startswith('harness__')})['demo-assistant']
        return {name: (base64.b64decode(raw), False) for name, raw in bundle.files.items()}

    def test_source_tools_and_tool_gate_behavior(self):
        result = subprocess.run(['node', '--test', '--test-isolation=none',
                                 str(ROOT / 'tests/test_saw_source_readers.mjs'),
                                 str(PATH.parent / 'tool-gate.test.mjs')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_harness_checks_all_three_governed_sources(self):
        tree = self.bundle()
        info = installer.describe_harness_tree(tree, inline=True)
        self.assertEqual(set(info['mcpServers']), {'mailpit_reader', 'mattermost_reader', 'calendar_reader', 'briefing_writer'})
        self.assertEqual({g['governanceProfile'] for g in info['governance']},
                         {'mailpit', 'mattermost', 'calendar'})
        self.assertEqual(tree['mcp/briefing-lib.mjs'][0],
                         (ROOT / 'tenant/saw/charts/saw-bom/harness/daily-briefing/mcp/briefing-lib.mjs').read_bytes())
        applier = installer.ProfileApplier.__new__(installer.ProfileApplier)
        applier.governance_catalog = lambda ws: {p: set() for p in ['mailpit', 'mattermost', 'calendar']}
        ws = installer.Workspace(name='default')
        providers = [installer.Provider(name=p, type=p) for p in ['mailpit', 'mattermost', 'calendar']]
        applier.usable = lambda ws: providers
        sandbox = installer.Sandbox(name='notebook', providers=[p.name for p in providers])
        applier.check_harness_governance(ws, sandbox, info)
        for missing in ['mailpit', 'mattermost', 'calendar']:
            sandbox.providers = [p.name for p in providers if p.name != missing]
            with self.assertRaisesRegex(installer.InstallerError, missing):
                applier.check_harness_governance(ws, sandbox, info)
        self.assertEqual(installer.openclaw_harness_config(info)['plugins.load.paths'], ['/sandbox/harness'])

    def test_briefing_writer_and_readers_share_the_upstream_store_inside_the_workspace(self):
        tree = self.bundle()
        self.assertIn('mcp/briefing-writer.mjs', tree)
        self.assertEqual(tree['mcp/briefing-writer.mjs'][0],
                         (ROOT / 'tenant/saw/charts/saw-bom/harness/daily-briefing/mcp/briefing-writer.mjs').read_bytes())
        servers = installer.harness_mcp_servers(installer.describe_harness_tree(tree, inline=True))
        self.assertEqual(servers['briefing_writer']['args'], ['/sandbox/harness/mcp/save-briefing.mjs'])
        self.assertFalse({'MM_TOKEN', 'MAILPIT_AUTHORIZATION', 'CALENDAR_AUTHORIZATION'}
                         & set(servers['briefing_writer']['env']))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, (content, _) in tree.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            store = root / 'briefing'
            requests = [
                {'id': 1, 'method': 'tools/list'},
                {'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'save_briefing', 'arguments': {'markdown': '# Daily briefing\nVerified sources.'}}},
                {'id': 3, 'method': 'tools/call', 'params': {
                    'name': 'save_briefing', 'arguments': {}}},
            ]
            result = subprocess.run(['node', str(root / 'mcp/save-briefing.mjs')],
                input='\n'.join(json.dumps(r) for r in requests) + '\n',
                env={**os.environ, 'BRIEFING_DIR': str(store)}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            replies = {r['id']: r for r in map(json.loads, result.stdout.splitlines())}
            self.assertEqual([t['name'] for t in replies[1]['result']['tools']], ['save_briefing'])
            schema = replies[1]['result']['tools'][0]['inputSchema']
            self.assertEqual(schema['required'], ['markdown'])
            self.assertEqual(schema['properties']['markdown']['type'], 'string')
            self.assertFalse(schema['additionalProperties'])
            self.assertTrue(replies[3]['result']['isError'])
            self.assertIn('Expected markdown', replies[3]['result']['content'][0]['text'])
            saved = json.loads(replies[2]['result']['content'][0]['text'])
            self.assertTrue(Path(saved['file']).is_relative_to(store))
            self.assertEqual(Path(saved['file']).read_text(), '# Daily briefing\nVerified sources.')

    def test_each_source_process_receives_only_its_provider_placeholder(self):
        servers = installer.harness_mcp_servers(installer.describe_harness_tree(self.bundle(), inline=True))
        credentials = {'mailpit_reader': 'MAILPIT_AUTHORIZATION',
                       'mattermost_reader': 'MM_TOKEN', 'calendar_reader': 'CALENDAR_AUTHORIZATION'}
        entrypoints = {'mailpit_reader': 'mailpit-reader.mjs',
                       'mattermost_reader': 'mattermost-reader.mjs', 'calendar_reader': 'calendar-reader.mjs'}
        for server, filename in entrypoints.items():
            self.assertEqual(servers[server]['args'], ['/sandbox/harness/mcp/' + filename])
        for server, name in credentials.items():
            env = servers[server]['env']
            self.assertEqual(env.get(name), '${' + name + '}')
            self.assertEqual(set(env) & set(credentials.values()), {name})

    def test_demo_inherits_existing_tool_settings_like_the_upstream_harness(self):
        info = installer.describe_harness_tree(self.bundle(), inline=True)
        self.assertEqual(info['toolAllowlist'], [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.openclaw'
            root.mkdir()
            config = root / 'openclaw.json'
            for before in [{'profile': 'full'}, {'allow': ['read'], 'toolSearch': True},
                           {'allow': ['read'], 'toolSearch': False}]:
                with self.subTest(before=before):
                    config.write_text(json.dumps({'tools': before}))
                    subprocess.run(['node', '-e', installer._OPENCLAW_PREFLIGHT.replace('/sandbox', directory),
                                    json.dumps({'channels': [], 'tools': info['toolAllowlist']})],
                                   check=True, capture_output=True)
                    self.assertEqual(json.loads(config.read_text())['tools'], before)
                    self.assertFalse((root / 'saw-harness-tools-before.json').exists())

    def test_rendered_mcp_process_initializes_lists_tools_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, (content, _) in self.bundle().items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            servers = [
                ('mailpit_reader', 'mailpit-reader.mjs', ['saw_read_email']),
                ('mattermost_reader', 'mattermost-reader.mjs', ['saw_read_chat']),
                ('calendar_reader', 'calendar-reader.mjs', ['saw_read_calendar']),
            ]
            for server, filename, tools in servers:
                with self.subTest(server=server):
                    requests = [
                        {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                        {'id': 2, 'method': 'tools/list'},
                        {'id': 3, 'method': 'tools/call', 'params': {
                            'name': tools[0], 'arguments': {'url': 'https://other.invalid'}}},
                        {'id': 4, 'method': 'tools/call', 'params': {'name': 'not_exposed', 'arguments': {}}},
                    ]
                    result = subprocess.run(['node', str(root / 'mcp' / filename)],
                        input='\n'.join(json.dumps(r) for r in requests) + '\n',
                        env={'PATH': os.environ['PATH']}, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    replies = {r['id']: r for r in map(json.loads, result.stdout.splitlines())}
                    self.assertEqual(replies[1]['result']['serverInfo']['name'], server)
                    self.assertEqual({t['name'] for t in replies[2]['result']['tools']}, set(tools))
                    self.assertEqual(replies[2]['result']['tools'][0]['inputSchema'],
                                     {'type': 'object', 'properties': {}, 'additionalProperties': False})
                    self.assertTrue(replies[3]['result']['isError'])
                    self.assertIn('accept no arguments', replies[3]['result']['content'][0]['text'])
                    self.assertIn('unknown tool', replies[4]['error']['message'])

    def test_gate_bundle_contains_only_enforcement_assets(self):
        applier = installer.ProfileApplier.__new__(installer.ProfileApplier)
        applier.cfg = {'dashboard': {'owner': 'user-test'}, 'ownerSubject': 'subject-test'}
        tree, _, _, _ = applier.tool_gate_bundle(installer.Workspace(name='default'), installer.Sandbox(name='notebook'))
        self.assertFalse(any(p.startswith('source-readers/') for p in tree))
        self.assertIn(b'defaultAction: deny', tree['tool-actions.yaml'][0])
        self.assertEqual((PATH.parent / 'tool-actions.yaml').read_bytes(),
                         (ROOT / 'tenant/saw/charts/governance-policy/tool-actions.yaml').read_bytes())

    def test_apply_restores_old_demo_tool_scope_without_overwriting_operator_changes(self):
        info = installer.describe_harness_tree(self.bundle(), inline=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.openclaw'
            root.mkdir()
            config = root / 'openclaw.json'
            managed = ['demo-assistant__saw_read_email']
            before = {'profile': 'full', 'allow': ['read']}
            def migrate(allow):
                config.write_text(json.dumps({'tools': {'profile': 'full', 'allow': allow, 'toolSearch': False},
                    'plugins': {'entries': {'saw-tool-gate': {'enabled': True}}}}))
                (root / 'saw-harness-tools-before.json').write_text(json.dumps({'before': before, 'managed': managed}))
                subprocess.run(['node', '-e', installer._OPENCLAW_PREFLIGHT.replace('/sandbox', directory),
                                json.dumps({'channels': ['mattermost'], 'tools': info['toolAllowlist']})],
                               check=True, capture_output=True)
                return json.loads(config.read_text())
            restored = migrate(managed)
            self.assertEqual(restored['tools'], before)
            self.assertFalse(restored['channels']['mattermost']['enabled'])
            self.assertTrue(restored['plugins']['entries']['saw-tool-gate']['enabled'])
            self.assertEqual(migrate(['operator-tool'])['tools']['allow'], ['operator-tool'])


if __name__ == '__main__':
    unittest.main()
