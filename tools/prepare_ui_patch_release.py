"""Merge the authorized UI update onto the public compatibility release baseline."""
from pathlib import Path
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1]
release = source.with_name('ADS Agent GitHub')
files = ['addon/ads_agent/panel.py', 'addon/ads_agent/uiscale.py',
         'backend/config.py', 'backend/server.py', 'tests/test_auth.py',
         'tests/test_project_isolation.py', 'tests/test_design_isolation.py']
for name in files:
    base = subprocess.check_output(['git', 'show', '9ee2c59:' + name], cwd=release)
    with tempfile.TemporaryDirectory(prefix='ads-ui-release-') as tmp:
        paths = [Path(tmp) / n for n in ('release', 'base', 'updated')]
        contents = [(release / name).read_bytes(), base, (source / name).read_bytes()]
        for path, content in zip(paths, contents):
            path.write_bytes(content.replace(b'\r\n', b'\n'))
        merged = subprocess.run(['git', 'merge-file', '-p', *map(str, paths)], capture_output=True)
        if merged.returncode < 0 or merged.returncode > 127:
            raise RuntimeError(merged.stderr.decode())
        (release / name).write_bytes(merged.stdout)
        print(name, 'conflicts=' + str(merged.returncode))
for name in ['tests/test_ui_navigation.py', 'tests/preview_model_settings.py']:
    (release / name).write_bytes((source / name).read_bytes())
    print(name, 'added')
