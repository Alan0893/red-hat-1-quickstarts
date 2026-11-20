# Unseal the workshop Vault after a restart (cluster stop/start, node reboot).
# Uses the key saw_prepare's bootstrap stored; does nothing if already unsealed.
import json
import os
import ssl
import sys
import urllib.request

url = os.environ['VAULT_URL']
context = ssl.create_default_context(cafile='/ca/service-ca.crt')


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + '/v1' + path, data=data, method='PUT' if body else 'GET',
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        return json.load(response)


status = call('/sys/seal-status')
if not status['initialized']:
    sys.exit('Vault is not initialized; nothing to unseal')
if not status['sealed']:
    print('Vault is unsealed')
    sys.exit(0)
with open('/state/init.json') as f:
    keys = json.load(f)['keys_base64']
for key in keys:
    status = call('/sys/unseal', {'key': key})
    if not status['sealed']:
        print('Vault unsealed')
        sys.exit(0)
sys.exit('Stored keys did not unseal Vault')
