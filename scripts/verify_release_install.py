"""Build and validate the exact source ZIP in a new ordinary wheel venv.

All generated state stays in a new work directory; existing environments are
never reused or removed. Network dependency installation is explicit here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_verified(archive, manifest, target):
    rows = manifest['files']
    expected = {'reelpilot/' + row['path']: row for row in rows}
    if len(expected) != len(rows):
        raise ValueError('Duplicate manifest path')
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('ZIP file set differs from release manifest')
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name:
                raise ValueError('Invalid source member path')
            info = bundle.getinfo(name)
            if info.file_size > 2_000_000 or info.file_size != expected[name]['bytes']:
                raise ValueError('Unexpected source member size: ' + name)
            content = bundle.read(info)
            if hashlib.sha256(content).hexdigest() != expected[name]['sha256']:
                raise ValueError('Source member hash mismatch: ' + name)
            destination = target.joinpath(*path.parts)
            if not destination.resolve().is_relative_to(target.resolve()):
                raise ValueError('Source extraction escaped work directory')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return target / 'reelpilot'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build-docker', action='store_true')
    args = parser.parse_args()
    archive, output = args.archive.resolve(), args.output.resolve()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    args.work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='wheel-', dir=args.work_root.resolve()))
    env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(name, None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    report = {
        'schema_version': 1,
        'status': 'failed',
        'source_zip_sha256': digest(archive),
        'source_files': len(manifest['files']),
        'source_members_verified': False,
        'venv_created_fresh': True,
        'container_built': False,
        'scope': 'Artifact installability and MCP discovery; not model quality or render readiness',
        'stages': [],
    }

    def run(label, command, cwd, *, timeout=600, capture=False):
        started = time.monotonic()
        with (work / (label + '.log')).open('w', encoding='utf-8') as log:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE if capture else log,
                                    stderr=log if capture else subprocess.STDOUT, text=True,
                                    errors='replace', timeout=timeout, check=False)
            if capture:
                log.write(result.stdout)
        report['stages'].append({'name': label, 'exit_code': result.returncode,
                                 'seconds': round(time.monotonic() - started, 3)})
        print(json.dumps(report['stages'][-1]), flush=True)
        if result.returncode:
            raise RuntimeError(label + ' failed; inspect its local work log')
        return result.stdout.strip() if capture else None

    try:
        source = extract_verified(archive, manifest, work / 'source')
        report['source_members_verified'] = True
        wheels = work / 'wheels'
        wheels.mkdir()
        outside = work / 'outside'
        outside.mkdir()
        run('wheel-build', [sys.executable, '-I', '-m', 'pip', '--disable-pip-version-check',
            'wheel', '--no-deps', '--index-url', 'https://pypi.org/simple',
            '--wheel-dir', str(wheels), str(source)], outside)
        built = list(wheels.glob('reelpilot-*.whl'))
        if len(built) != 1:
            raise RuntimeError('Expected exactly one ReelPilot wheel')
        wheel = built[0]
        venv = work / 'venv'
        run('venv-create', [sys.executable, '-I', '-m', 'venv', str(venv)], outside)
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        run('wheel-install', [str(python), '-I', '-m', 'pip', '--disable-pip-version-check',
            'install', '--index-url', 'https://pypi.org/simple', str(wheel) + '[mcp]'], outside)
        run('pip-check', [str(python), '-I', '-m', 'pip', 'check'], outside)
        validation = work / 'installed-validation.json'
        command = [str(python), '-I', str(source / 'scripts/validate_distribution.py'),
                   '--legacy', '--output', str(validation)]
        if args.build_docker:
            image = 'reelpilot-mcp:distribution-' + uuid.uuid4().hex
            run('docker-build', ['docker', 'build', '--file', str(source / 'Dockerfile.mcp'),
                                '--tag', image, str(source)], outside, timeout=1200)
            report['container_built'] = True
            report['container_image_id'] = run('docker-image-id',
                ['docker', 'image', 'inspect', '--format', '{{.Id}}', image], outside, capture=True)
            report['container_configured_user'] = run('docker-image-user',
                ['docker', 'image', 'inspect', '--format', '{{.Config.User}}', image], outside, capture=True)
            if report['container_configured_user'] != '65532:65532':
                raise RuntimeError('MCP image does not use the intended unprivileged user')
            command += ['--docker-image', image]
        run('installed-smoke', command, outside, timeout=180)
        report['installed_validation'] = json.loads(validation.read_text(encoding='utf-8'))
        if report['installed_validation']['status'] != 'passed':
            raise RuntimeError('Installed distribution reported failure')
        report['wheel'] = {'filename': wheel.name, 'sha256': digest(wheel), 'bytes': wheel.stat().st_size}
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wheel, output.parent / wheel.name)
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'source_members_verified': report['source_members_verified'],
                      'container_built': report['container_built']}), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
