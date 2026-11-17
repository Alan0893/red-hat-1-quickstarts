"""Render shared demo charts and the tenant provisioning chain."""

import json
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(chart, values):
    text = subprocess.check_output(
        ['helm', 'template', 'saw-demo', str(ROOT / chart), '--namespace', 'openshell-agents', '-f', '-'],
        input=yaml.safe_dump(values),
        text=True,
    )
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


class DemoChartTests(unittest.TestCase):
    def test_mutable_demo_image_overrides_are_rejected(self):
        for chart, key in [('mailpit', 'image'), ('mattermost', 'image'),
                           ('mattermost', 'postgresImage'), ('radicale', 'image')]:
            result = subprocess.run(['helm', 'template', 'test', str(ROOT / 'infra/saw-demo/charts' / chart),
                                     '--set', key + '=example.invalid/unreviewed:latest',
                                     '--set', 'postgresCredentials.password=example-only'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, chart + ': ' + key)
            self.assertIn('must be pinned by digest', result.stderr)

    def test_demo_https_routes_and_profiles_share_exact_hosts(self):
        values = {'saw': {'enabled': True, 'clusterDomain': 'test.example',
                         'keycloak': {'postgresPassword': 'example-only'},
                         'demo': {'enabled': True, 'calendarEnabled': True}}}
        docs = render(ROOT / 'infra/bootstrap', values)
        profiles = next(d['data'] for d in docs if d['kind'] == 'ConfigMap'
                        and d['metadata']['name'] == 'governance-interceptor-extra-profiles')
        for service, profile in [('mailpit', 'mailpit'), ('mattermost', 'mattermost'), ('radicale', 'calendar')]:
            app = next(d for d in docs if d['metadata']['name'] == 'saw-demo-' + service)
            chart_values = yaml.safe_load(app['spec']['source']['helm'].get('values', '{}'))
            chart = ROOT.parents[1] / app['spec']['source']['path']
            routes = [d for d in render(chart, chart_values) if d['kind'] == 'Route']
            self.assertEqual(len(routes), 1)
            route = routes[0]['spec']
            host = service + '-ui-openshell-agents.apps.test.example'
            self.assertEqual(route['host'], host)
            self.assertEqual(route['tls'], {'termination': 'edge', 'insecureEdgeTerminationPolicy': 'Redirect'})
            endpoints = yaml.safe_load(profiles[profile + '.yaml'])['endpoints']
            self.assertIn((host, 443), [(e['host'], e['port']) for e in endpoints])
            self.assertFalse(any(e['port'] in (8025, 8065, 5232) for e in endpoints))
            # Route activation must not restart pods with ephemeral source data.
            deployments = lambda items: [d for d in items if d['kind'] == 'Deployment']
            self.assertEqual(deployments(render(chart, {'postgresCredentials': {'password': 'example-only'}})),
                             deployments(render(chart, chart_values)))

    def test_demo_profiles_are_opt_in_and_preserve_inference(self):
        chart = ROOT / 'infra/bootstrap'
        values = {'saw': {'enabled': True, 'clusterDomain': 'test.example',
                         'keycloak': {'postgresPassword': 'example-only'},
                         'governance': {'customInference': {'host': 'model.example'}}}}
        def profiles():
            docs = render(chart, values)
            return next(d['data'] for d in docs if d['kind'] == 'ConfigMap'
                        and d['metadata']['name'] == 'governance-interceptor-extra-profiles')
        self.assertEqual(set(profiles()), {'custom-inference.yaml'})
        values['saw']['demo'] = {'enabled': True}
        result = profiles()
        self.assertEqual(set(result), {'custom-inference.yaml', 'mailpit.yaml', 'mattermost.yaml'})
        self.assertEqual(yaml.safe_load(result['custom-inference.yaml'])['endpoints'][0]['host'], 'model.example')
        values['saw']['demo']['calendarEnabled'] = True
        self.assertIn('calendar.yaml', profiles())
        values['saw']['demo']['enabled'] = 'True'
        self.assertEqual(set(profiles()), {'custom-inference.yaml', 'mailpit.yaml', 'mattermost.yaml', 'calendar.yaml'})
        values['saw']['governance']['customInference']['host'] = ''
        self.assertEqual(set(profiles()), {'mailpit.yaml', 'mattermost.yaml', 'calendar.yaml'})

    def test_shared_services_are_opt_in_and_track_the_infra_revision(self):
        values = {'saw': {'enabled': True, 'clusterDomain': 'test.example',
                         'git': {'revision': 'feat/saw-personal-assistant'},
                         'keycloak': {'postgresPassword': 'example-only'}}}
        def docs():
            return render(ROOT / 'infra/bootstrap', values)
        self.assertFalse(any(d['metadata']['name'].startswith('saw-demo-') for d in docs()))
        values['saw']['demo'] = {'enabled': True}
        apps = [d for d in docs() if d['kind'] == 'Application' and d['metadata']['name'].startswith('saw-demo-')]
        self.assertEqual({d['metadata']['name'] for d in apps}, {'saw-demo-mailpit', 'saw-demo-mattermost', 'saw-demo-radicale'})
        for app in apps:
            self.assertEqual(app['spec']['source']['targetRevision'], 'feat/saw-personal-assistant')
            self.assertEqual(app['spec']['destination']['namespace'], 'openshell-agents')
            service = app['metadata']['name'].removeprefix('saw-demo-')
            self.assertEqual(app['spec']['source']['path'], 'automation/gitops/infra/saw-demo/charts/' + service)
        bindings = [d for d in docs() if d['kind'] == 'RoleBinding']
        binding = next(d for d in bindings if d['metadata']['name'] == 'saw-demo-anyuid')
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': 'saw-demo', 'namespace': 'openshell-agents'}])

    def test_demo_charts_use_pinned_images_and_explicit_privileged_account(self):
        for name in ('mailpit', 'mattermost', 'radicale'):
            with self.subTest(chart=name):
                docs = render(ROOT / 'infra/saw-demo/charts' / name,
                              {'postgresCredentials': {'password': 'example-only'}})
                self.assertFalse(any(d['kind'] == 'Route' for d in docs))
                for doc in docs:
                    if doc['kind'] == 'Deployment':
                        pod = doc['spec']['template']['spec']
                        if name != 'mailpit':
                            self.assertEqual(pod['serviceAccountName'], 'saw-demo')
                        for container in pod.get('containers', []) + pod.get('initContainers', []):
                            self.assertRegex(container['image'], r'@sha256:[a-f0-9]{64}$')

    def test_tenant_demo_egress_preserves_existing_rules_and_policy_mode(self):
        existing = {
            'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'models'}}}],
            'ports': [{'protocol': 'TCP', 'port': 8000}],
        }
        values = {
            'rag': None,
            'saw': {
                'enabled': True,
                'userName': 'user-test',
                'clusterDomain': 'test.example',
                'credentialProxyImage': 'example.invalid/proxy@sha256:' + 'a' * 64,
                'vmSource': {'dataSource': 'golden'},
                'values': {
                    'defaults': {
                        'openshellSaw': {
                            'egress': {'extraAllow': ['model.example']},
                            'networkPolicy': {'enabled': True, 'extraEgress': [existing]},
                        }
                    }
                },
            },
        }

        def settings():
            docs = render(ROOT / 'tenant/bootstrap', values)
            app = next(d for d in docs if d['kind'] == 'Application')
            return yaml.safe_load(app['spec']['source']['helm']['values'])['defaults']['openshellSaw']

        self.assertEqual(settings()['networkPolicy']['extraEgress'], [existing])
        values['saw']['demo'] = {'enabled': True}
        config = settings()
        result = config['networkPolicy']
        self.assertTrue(result['enabled'])
        self.assertEqual(result['extraEgress'], [existing])
        self.assertEqual(
            set(config['egress']['extraAllow']),
            {'model.example'}
            | {name + '-ui-openshell-agents.apps.test.example' for name in ('mailpit', 'mattermost', 'radicale')},
        )
        values['saw']['values']['defaults']['openshellSaw']['networkPolicy']['enabled'] = False
        self.assertFalse(settings()['networkPolicy']['enabled'])


class MattermostDatabaseTests(unittest.TestCase):
    chart = ROOT / 'infra/saw-demo/charts/mattermost'

    def resources(self):
        return {(d['kind'], d['metadata']['name']): d for d in
                render(self.chart, {'postgresCredentials': {'password': 'provisioned-password'}})}

    def test_database_uses_the_provisioned_password_without_a_generator(self):
        docs = self.resources()
        self.assertEqual(docs, self.resources())
        secret = docs['Secret', 'mattermost-postgres-credentials']
        self.assertEqual(secret['stringData']['password'], 'provisioned-password')
        self.assertEqual(secret['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-2')
        self.assertFalse(any(kind in ('Job', 'ServiceAccount', 'Role', 'RoleBinding', 'ConfigMap')
                             for kind, name in docs))
        for name, variable, key in (('mattermost', 'MM_SQLSETTINGS_DATASOURCE', 'datasource'),
                                    ('mattermost-postgres', 'POSTGRESQL_PASSWORD', 'password')):
            env = docs['Deployment', name]['spec']['template']['spec']['containers'][0]['env']
            password = next(e for e in env if e['name'] == variable)
            self.assertEqual(password['valueFrom']['secretKeyRef'], {
                'name': 'mattermost-postgres-credentials', 'key': key,
            })
            self.assertNotIn('mmuser_password', str(env))
        # Only the Mattermost server needs the anyuid exception; its database
        # must stay inside restricted-v2 so the demo keeps one SCC exception.
        database = docs['Deployment', 'mattermost-postgres']['spec']['template']['spec']
        self.assertTrue(database['securityContext']['runAsNonRoot'])
        self.assertNotIn('runAsUser', database['securityContext'])
        self.assertNotIn('runAsUser', database['containers'][0]['securityContext'])

    def test_database_password_special_characters_survive_the_connection_url(self):
        password = 'pass with:/?#@%+$chars'
        docs = render(self.chart, {'postgresCredentials': {'password': password}})
        secret = next(d for d in docs if d['kind'] == 'Secret')
        self.assertEqual(secret['stringData']['password'], password)
        connection = urlsplit(secret['stringData']['datasource'])
        self.assertEqual(unquote(connection.password), password)
        self.assertEqual(connection.hostname, 'mattermost-postgres')
        self.assertEqual(connection.path, '/mattermost')
        self.assertEqual(connection.query, 'sslmode=disable&connect_timeout=10')
        self.assertEqual(connection.fragment, '')

    def test_database_survives_pod_replacement_and_accepts_only_its_mattermost(self):
        docs = self.resources()
        pvc = docs.get(('PersistentVolumeClaim', 'mattermost-postgres'))
        self.assertIsNotNone(pvc, 'shared tenant accounts and sessions must survive database pod replacement')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '1Gi')
        self.assertEqual(pvc['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertNotIn('storageClassName', pvc['spec'])
        deployment = docs['Deployment', 'mattermost-postgres']['spec']
        self.assertEqual(deployment['strategy']['type'], 'Recreate')
        pod = deployment['template']['spec']
        self.assertEqual(pod['volumes'], [
            {'name': 'data', 'persistentVolumeClaim': {'claimName': 'mattermost-postgres'}},
        ])
        self.assertEqual(pod['containers'][0]['volumeMounts'], [
            {'name': 'data', 'mountPath': '/var/lib/pgsql/data'},
        ])
        policy = docs['NetworkPolicy', 'mattermost-postgres']['spec']
        self.assertEqual(policy['policyTypes'], ['Ingress'])
        self.assertEqual(policy['podSelector']['matchLabels'], {
            'app.kubernetes.io/name': 'mattermost-postgres', 'app.kubernetes.io/instance': 'saw-demo',
        })
        self.assertEqual(policy['ingress'], [{
            'from': [{'podSelector': {'matchLabels': {
                'app.kubernetes.io/name': 'mattermost', 'app.kubernetes.io/instance': 'saw-demo',
            }}}], 'ports': [{'protocol': 'TCP', 'port': 5432}],
        }])

    def test_infra_passes_its_existing_provisioned_password_to_mattermost(self):
        docs = render('infra/bootstrap', {'saw': {
            'enabled': True, 'clusterDomain': 'test.example',
            'keycloak': {'postgresPassword': 'provisioned-password'},
            'demo': {'enabled': True},
        }})
        app = next(d for d in docs if d['metadata']['name'] == 'saw-demo-mattermost')
        values = yaml.safe_load(app['spec']['source']['helm']['values'])
        secret = next(d for d in render(self.chart, values)
                      if d['kind'] == 'Secret' and d['metadata']['name'] == 'mattermost-postgres-credentials')
        self.assertEqual(secret['stringData']['password'], 'provisioned-password')

    def test_standalone_chart_requires_a_provisioned_database_password(self):
        result = subprocess.run(['helm', 'template', 'test', str(self.chart)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('postgresCredentials.password is required', result.stderr)


class DemoProvisioningTests(unittest.TestCase):
    def tenant_values(self):
        return {'rag': None, 'saw': {'enabled': True, 'userName': 'user-test', 'clusterDomain': 'test.example',
                'credentialProxyImage': 'example.invalid/proxy@sha256:' + 'a' * 64, 'vmSource': {'dataSource': 'golden'},
                'account': {'enabled': True, 'password': 'ProvisionedPassword123!'},
                'inferenceSeed': {'enabled': True, 'secretData': {'api_key': 'synthetic-key', 'url': 'https://model.example/v1', 'model': 'existing-model'}},
                'inferenceHost': 'model.example', 'demo': {'enabled': True, 'provisioningEnabled': True}}}

    def test_tenant_job_uses_provisioned_identity_and_secret_mount(self):
        docs = render('tenant/bootstrap', self.tenant_values())
        job = next((d for d in docs if d['kind'] == 'Job' and d['metadata']['name'] == 'saw-user-test-demo-seed'), None)
        self.assertIsNotNone(job, 'tenant demo seed Job must be rendered')
        self.assertEqual(job['metadata']['namespace'], 'vault')
        pod = job['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertIn({'name': 'SAW_USER', 'value': 'user-test'}, container['env'])
        self.assertNotIn('ProvisionedPassword123!', str(container))
        self.assertTrue(any(v.get('secret', {}).get('secretName') == 'saw-user-test-demo-input' for v in pod['volumes']))
        self.assertEqual(job['metadata']['annotations']['argocd.argoproj.io/hook'], 'Sync')
        rules = next(d['rules'] for d in docs if d['kind'] == 'Role' and d['metadata']['name'] == 'saw-user-test-demo-seed')
        self.assertTrue(all(r.get('resourceNames') for r in rules))

    def test_two_tenants_keep_their_lab_passwords_and_source_credentials_separate(self):
        for user in ['user-first', 'user-second']:
            values = self.tenant_values()
            values['saw']['userName'] = user
            values['saw']['account']['password'] = user + '-existing-password'
            docs = render('tenant/bootstrap', values)
            account = next(d for d in docs if d['kind'] == 'Secret' and d['metadata']['name'] == f'saw-{user}-account')
            source = next(d for d in docs if d['kind'] == 'Secret' and d['metadata']['name'] == f'saw-{user}-demo-input')
            self.assertEqual(source['stringData']['password'], account['stringData']['password'])
            app = next(d for d in docs if d['kind'] == 'Application')
            child = render('tenant/saw/charts/saw-users', yaml.safe_load(app['spec']['source']['helm']['values']))
            secret_app = next(d for d in child if d['kind'] == 'Application' and d['spec']['source']['path'].endswith('/pattern-secrets'))
            secrets = render('tenant/saw/charts/pattern-secrets', yaml.safe_load(secret_app['spec']['source']['helm']['values']))
            for provider in ['inference', 'mailpit', 'mattermost', 'calendar']:
                secret = next(d for d in secrets if d['metadata']['name'] == provider)
                self.assertEqual(secret['spec']['dataFrom'][0]['extract']['key'], f'secret/data/hub/saw-{user}/{provider}')

    def test_generated_bom_and_proxy_attach_all_sources_without_manual_channel_id(self):
        docs = render('tenant/bootstrap', self.tenant_values())
        app = next(d for d in docs if d['kind'] == 'Application')
        values = yaml.safe_load(app['spec']['source']['helm']['values'])
        self.assertEqual(values['defaults']['profiles'], ['demo-assistant'])
        self.assertEqual(values['defaults']['openshellSaw']['sourceReaders']['tenantUser'], 'user-test')
        self.assertTrue(values['defaults']['openshellSaw']['sourceReaders']['enabled'])
        child = render('tenant/saw/charts/saw-users', values)
        settings = {
            d['spec']['source']['path'].rsplit('/', 1)[-1]: yaml.safe_load(d['spec']['source']['helm']['values'])
            for d in child
            if d['kind'] == 'Application'
        }
        self.assertEqual(
            set(settings['pattern-secrets']['secrets']), {'inference', 'mailpit', 'calendar', 'mattermost'}
        )
        bindings = settings['openshell-saw']['credentialProxy']['secrets']
        for source in ['mailpit', 'calendar', 'mattermost']:
            self.assertIn({'name': source, 'key': 'api_key', 'providers': [source]}, bindings)
        machine = render('tenant/saw/charts/openshell-saw', settings['openshell-saw'])
        proxy = next(
            d
            for d in machine
            if d['kind'] == 'ConfigMap' and d['metadata']['name'].endswith('-credential-proxy-config')
        )
        policy = json.loads(proxy['data']['config.json'])
        for source, service in [('mailpit', 'mailpit'), ('mattermost', 'mattermost'), ('calendar', 'radicale')]:
            self.assertEqual(policy['profiles'].get(source), [service + '-ui-openshell-agents.apps.test.example'])
        bad = dict(settings['openshell-saw'])
        bad['egress'] = {'extraAllow': ['model.example']}
        denied = subprocess.run(
            ['helm', 'template', 'test', str(ROOT / 'tenant/saw/charts/openshell-saw'), '-f', '-'],
            input=yaml.safe_dump(bad),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('demo credential hosts must also be allowed', denied.stderr)
        bom = render('tenant/saw/charts/saw-bom', settings['saw-bom'])
        data = next(d['data'] for d in bom if d['kind'] == 'ConfigMap')
        providers = yaml.safe_load(next(v for k, v in data.items() if k.endswith('providers.yaml')))['spec'][
            'providers'
        ]
        self.assertEqual({p['name'] for p in providers}, {'custom-inference', 'mailpit', 'mattermost', 'calendar'})
        sandbox = yaml.safe_load(next(v for k, v in data.items() if k.endswith('sandbox.yaml')))['spec']['sandboxes'][0]
        self.assertTrue({'mailpit', 'mattermost', 'calendar'} <= set(sandbox['providers']))
        self.assertEqual(sandbox.get('harnessRef', {}).get('name'), 'demo-assistant')
        self.assertTrue(settings['openshell-saw']['allowDriverConfig'])
        self.assertIn('harness-index.yaml', data)

    def test_demo_preserves_the_upstream_direct_credential_mode(self):
        values = self.tenant_values()
        values['saw']['values'] = {'defaults': {'openshellSaw': {'credentialProxy': {'enabled': False}}}}
        docs = render('tenant/bootstrap', values)
        app = next(d for d in docs if d['kind'] == 'Application')
        children = render('tenant/saw/charts/saw-users', yaml.safe_load(app['spec']['source']['helm']['values']))
        workspace = next(d for d in children if d['kind'] == 'Application' and d['spec']['source']['path'].endswith('/openshell-saw'))
        machine = render('tenant/saw/charts/openshell-saw', yaml.safe_load(workspace['spec']['source']['helm']['values']))
        self.assertFalse(any(d['metadata']['name'].endswith('-credential-proxy-config') for d in machine))

    def test_infra_authenticated_routes_and_readonly_profiles_have_no_backend_bypass(self):
        values = {'saw': {'enabled': True, 'clusterDomain': 'test.example', 'keycloak': {'postgresPassword': 'example-only'},
                          'demo': {'enabled': True, 'calendarEnabled': True, 'provisioningEnabled': True}}}
        docs = render('infra/bootstrap', values)
        names = {(d['kind'],d['metadata']['name']):d for d in docs}
        self.assertIn(('Job','saw-demo-admin'), names)
        gateways = [d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'].startswith('saw-demo-')]
        self.assertEqual(len(gateways), 1, 'Both source views must share one identity-login queue')
        self.assertEqual(gateways[0]['spec']['strategy']['type'], 'Recreate')
        ports = gateways[0]['spec']['template']['spec']['containers'][0]['ports']
        self.assertEqual({p['name']: p['containerPort'] for p in ports}, {'mailpit': 8080, 'calendar': 8081})
        ca = names.get(('ExternalSecret', 'saw-demo-ingress-ca'))
        self.assertIsNotNone(ca, 'gateway must reuse the ingress CA stored in Vault')
        self.assertEqual(ca['spec']['data'][0]['remoteRef']['key'], 'secret/data/hub/cluster-ingress-ca')
        for service in ['mailpit', 'radicale']:
            port = names['Service', 'saw-demo-' + service + '-access']['spec']['ports'][0]
            self.assertEqual(port['targetPort'], 'mailpit' if service == 'mailpit' else 'calendar')
            app = names['Application', 'saw-demo-' + service]
            child_values = yaml.safe_load(app['spec']['source']['helm']['values'])
            chart = ROOT.parents[1] / app['spec']['source']['path']
            route = next(d for d in render(chart, child_values) if d['kind'] == 'Route')
            self.assertEqual(route['spec']['to']['name'], 'saw-demo-' + service + '-access')
            self.assertIn(('NetworkPolicy','saw-demo-' + service + '-backend'), names)
        profiles = names['ConfigMap','governance-interceptor-extra-profiles']['data']
        for source in ['mailpit','calendar']:
            profile = yaml.safe_load(profiles[source + '.yaml'])
            self.assertEqual(profile['credentials'][0]['auth_style'], 'header')
            self.assertEqual(profile['credentials'][0]['header_name'], 'authorization')
            self.assertEqual(len(profile['endpoints']),1)
        for source in ['mailpit', 'mattermost', 'calendar']:
            endpoint = yaml.safe_load(profiles[source + '.yaml'])['endpoints'][0]
            self.assertNotIn('rules', endpoint)
            self.assertEqual(endpoint['access'], 'read-only')

    def test_provisioning_requires_the_existing_lab_account(self):
        values = self.tenant_values()
        values['saw']['account']['enabled'] = False
        result = subprocess.run(['helm','template','test',str(ROOT/'tenant/bootstrap'),'-f','-'],input=yaml.safe_dump(values),text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('saw.account.enabled',result.stderr)

    def test_demo_rejects_unsupported_container_mode_before_provisioning(self):
        values = self.tenant_values()
        values['saw'].update({'deploymentMode': 'container', 'ownerSubject': 'subject-test',
                              'registration': 'a' * 16, 'installationID': 'installation-test',
                              'profile': 'data-science', 'containerSite': {'subject': 'subject-test'}})
        result = subprocess.run(['helm', 'template', 'test', str(ROOT / 'tenant/bootstrap'), '-f', '-'],
                                input=yaml.safe_dump(values), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('personal-assistant demo requires saw.deploymentMode=vm', result.stderr)


if __name__ == '__main__':
    unittest.main()
