"""Export a clean source checkout without copying Git history or user data.

Usage: python tools/export_source.py --out "D:\\Antenna\\ADS Agent GitHub"
Existing destinations are refused. Continue development in the exported checkout.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release_manifest as manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    destination = Path(args.out).resolve()
    if destination.exists():
        parser.error('Destination already exists; no files were overwritten')
    if destination == ROOT or ROOT in destination.parents:
        parser.error('Destination must be outside the original project')

    required = ['.gitignore', '.gitattributes', 'README.md', 'CHANGELOG.md',
                'CONTRIBUTING.md', 'THIRD-PARTY-NOTICES.txt',
                'config.example.ini', 'release_manifest.py', 'install_addon.py',
                'check_env.py', 'install_addon.bat', 'uninstall_addon.bat',
                'start_backend.bat', 'selfcheck.bat',
                'tools/build_release.py', 'tools/export_source.py',
                'tests/_harness.py', 'tests/run_tests.py',
                'tests/fm_sc_case.py', 'tests/ce_amp_case.py', 'tests/cb_amp_case.py',
                'tests/iterate_ml_sym_metrics.py',
                'docs/安装说明.md', 'docs/版本迭代与发布.md',
                'docs/原理图建图与仿真经验总结.md', 'docs/Layout 审查设计.md']
    required += ['backend/' + name for name in manifest.BACKEND_FILES]
    required += ['addon/ads_agent/' + name for name in manifest.ADDON_FILES]
    required += [p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests').glob('test_*.py')
                 if p.name != 'test_loopback.py']
    required += [p.relative_to(ROOT).as_posix() for p in (ROOT / '.github').rglob('*') if p.is_file()]
    required += [p.relative_to(ROOT).as_posix() for p in (ROOT / 'docs/releases').glob('*.md')]
    required += [p.relative_to(ROOT).as_posix() for p in (ROOT / 'docs').glob('验证记录-*.md')]
    required = sorted(set(required))
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        parser.error('Missing source files: ' + ', '.join(missing))
    for name in required:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    print(f'Exported {len(required)} source files to {destination}')
    print('Original project and Git history remain unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
