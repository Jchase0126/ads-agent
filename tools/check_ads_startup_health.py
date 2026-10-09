"""Run inside ADS: verify health reuse, without exposing credentials or spawning services."""
import importlib
import json
import backend_launcher

importlib.reload(backend_launcher)
result = backend_launcher.probe_backend(timeout=3)
assert result['verdict']['usable'], result['verdict']['detail']
print(json.dumps({'reachable': result['reachable'],
                  'usable': result['verdict']['usable'],
                  'reason': result['verdict']['reason']}, ensure_ascii=False))
