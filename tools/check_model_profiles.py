"""Read-only live backend probe; never print API credentials."""
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'addon', 'ads_agent'))
import panel


def call(path, payload=None):
    headers = dict(panel._auth_header(), **{'Content-Type': 'application/json'})
    req = panel.urllib.request.Request(panel._backend_base() + path, headers=headers,
                                      data=None if payload is None else json.dumps(payload).encode())
    return json.load(panel._LOOPBACK.open(req, timeout=5))


health = call('/health')
print(json.dumps({k: health.get(k) for k in ('service', 'version', 'identity', 'active_turns')}, ensure_ascii=False))
config = call('/config')
print(json.dumps({'model': config.get('model'), 'has_key': config.get('has_key'),
                  'profile_labels': config.get('profile_labels')}, ensure_ascii=False))
try:
    profile = call('/config/model', {'model': config.get('model')})
    print(json.dumps({'profile_ok': True, 'has_address': bool(profile.get('base_url')),
                      'has_key': bool(profile.get('api_key')), 'provider_name': profile.get('provider_name')}))
except urllib.error.HTTPError as error:
    print('profile_http_status', error.code)
