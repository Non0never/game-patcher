import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import struct
import zipfile

spec = importlib.util.spec_from_file_location('release_updater', Path(__file__).with_name('updater.py'))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def build(root, tag, platform, output, settings=None):
    updater.PLATFORM = platform
    updater.configure(root, settings)
    updater.version(tag)
    files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    payload = {}
    for name in sorted(filter(None, files)):
        parts = Path(name).parts
        if any(part.startswith('.') or part == '__pycache__' for part in parts) or name.endswith(('.pyc', '.pyo')):
            continue
        if platform == 'windows' and (parts[0] in ('Game', 'Game_Linux', 'start.sh', 'lib64') or name.endswith('.so') or name.lower().endswith('.appimage')):
            continue
        if platform == 'linux' and '/bin/windows/' in name:
            continue
        updater.safe_name(name)
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing file or symbolic link: {name}')
        payload[name] = path.read_bytes()
    if any(name.startswith('Plugins/') and name.endswith('.rb') for name in payload):
        cache = root / 'Data/PluginScripts.rxdata'
        if not cache.is_file() or cache.is_symlink() or cache.stat().st_size <= 4:
            raise ValueError('The plugin cache is missing or empty. Start the game in Debug mode and recompile the plugins first.')
        payload['Data/PluginScripts.rxdata'] = cache.read_bytes()
    required = updater.game_files(payload, read=payload.__getitem__)
    if not set(required).issubset(payload):
        raise ValueError(f'Missing game runtime: {required}')
    if platform == 'windows':
        exe = next((payload[name] for name in required if name.lower().endswith('.exe')), b'')
        offset = int.from_bytes(exe[60:64], 'little')
        if exe[:2] != b'MZ' or exe[offset:offset + 4] != b'PE\0\0':
            raise ValueError('Windows packages require a real Windows executable.')
    payload['update-version.json'] = (json.dumps({'version': tag}) + '\n').encode()
    manifest = {'format': 1, 'repo': updater.REPO, 'version': tag, 'platform': platform, 'game_files': required, 'files': {}}
    for name, data in payload.items():
        path = root / name
        manifest['files'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                   'executable': (platform == 'linux' and name in required) or (path.exists() and bool(path.stat().st_mode & 0o111))}
    updater.validate_manifest(manifest, tag)
    output.mkdir(parents=True, exist_ok=True)
    target = output / updater.ASSET
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for name, data in payload.items():
            package.writestr(name, data)
        package.writestr('update-manifest.json', json.dumps(manifest, ensure_ascii=False))
    index = {**manifest, 'format': 2, 'archive': {'name': updater.ASSET, 'size': target.stat().st_size, 'sha256': updater.sha(target)}}
    with zipfile.ZipFile(target) as package, open(target, 'rb') as raw:
        for name, info in index['files'].items():
            entry = package.getinfo(name)
            raw.seek(entry.header_offset + 26)
            name_size, extra_size = struct.unpack('<HH', raw.read(4))
            info.update(offset=entry.header_offset + 30 + name_size + extra_size,
                        compressed_size=entry.compress_size, compression=entry.compress_type)
    target.with_name(target.name + '.manifest.json').write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    print(f'{target}: {len(payload)} files, {target.stat().st_size / 1024**2:.1f} MB')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--version', required=True)
    parser.add_argument('--platform', choices=['linux', 'windows'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--settings', type=Path)
    args = parser.parse_args()
    build(args.root.resolve(), args.version, args.platform, args.output, args.settings)
