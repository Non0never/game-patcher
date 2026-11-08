#!/usr/bin/env python3
import argparse
import contextlib
import concurrent.futures
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib

REPO = 'OWNER/REPOSITORY'
ASSET = 'game-linux.zip'
PLATFORM = 'windows' if os.name == 'nt' else 'linux'
CONFIG = {}
CONFIG_FILE = None
STATE = '.update-installed.json'
MAX_BYTES = 4 * 1024**3
VERSION_RE = re.compile(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def version(value):
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError('Release versions must use vMAJOR.MINOR.PATCH.')
    return tuple(map(int, match.groups()))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def configure(root, path=None):
    global CONFIG, CONFIG_FILE, REPO, ASSET
    path = path or Path(__file__).resolve().parents[1] / 'settings.json'
    CONFIG_FILE = Path(path).resolve()
    CONFIG = read_json(path)
    REPO = CONFIG['repository']
    if REPO == 'OWNER/REPOSITORY':
        raise ValueError('Set repository in settings.json first.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPO):
        raise ValueError('Invalid GitHub repository.')
    ASSET = CONFIG['assets'][PLATFORM]
    if Path(ASSET).name != ASSET or not ASSET.endswith('.zip'):
        raise ValueError('Invalid release asset filename.')


@contextlib.contextmanager
def install_lock(root):
    with open(root / '.update.lock', 'a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                if lock.seek(0, 2) == 0:
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another updater is already running.') from exc
        yield


def launch_windows(root, pid, config_path):
    # Run outside the game directory so Windows can replace the bundled runtime.
    target = Path(tempfile.mkdtemp(prefix='game-patcher-'))
    python_dir = Path(sys.executable).parent
    shutil.copytree(python_dir, target / 'python')
    shutil.copy2(Path(__file__), target / 'updater.py')
    python = target / 'python' / 'python.exe'
    args = [str(python), '-I', str(target / 'updater.py'), 'install', '--root', str(root), '--config', str(config_path)]
    if pid:
        args += ['--wait-pid', str(pid)]
    subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


def api(endpoint):
    if shutil.which('gh'):
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True, timeout=18,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            if 'HTTP 404' in result.stderr:
                raise FileNotFoundError(endpoint)
            raise RuntimeError('Cannot reach GitHub or authentication is missing. Sign in with gh auth login.')
        return json.loads(result.stdout)
    request = urllib.request.Request('https://api.github.com/' + endpoint,
                                    headers={'User-Agent': 'Game-Patcher'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(endpoint) from exc
        raise


def check(root):
    installed = (read_json(root / 'update-version.json')['version'] if (root / 'update-version.json').exists() else CONFIG.get('initial_version', '0.1.0'))
    try:
        release = api(f'repos/{REPO}/releases/latest')
    except FileNotFoundError:
        try:
            api(f'repos/{REPO}')
        except FileNotFoundError as exc:
            raise RuntimeError('Cannot access the private update repository. Check your GitHub login and repository access.') from exc
        return {'status': 'current', 'installed': installed, 'reason': 'No release has been published yet.'}
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease') or version(tag) <= version(installed):
        return {'status': 'current', 'installed': installed}
    asset = next((a for a in release.get('assets', []) if a['name'] == ASSET and a.get('state') == 'uploaded'), None)
    if not asset:
        return {'status': 'current', 'installed': installed, 'reason': 'The release package is still being prepared.'}
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise RuntimeError('The release package has no valid GitHub SHA256 digest.')
    index = next((a for a in release.get('assets', []) if a['name'] == ASSET + '.manifest.json' and a.get('state') == 'uploaded'), None)
    result = {'status': 'available', 'installed': installed, 'version': tag,
            'asset_id': asset['id'], 'size': asset['size'], 'digest': digest,
            'download_url': asset['browser_download_url']}
    if index:
        result['manifest'] = {'version': tag, 'asset_id': index['id'], 'size': index['size'],
                              'digest': index.get('digest', ''), 'download_url': index['browser_download_url']}
    return result


def safe_name(name):
    path = PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError(f'Invalid package path: {name}')
    if path.parts[0].startswith('.') or any(p.startswith('.git') for p in path.parts):
        raise ValueError(f'Protected package path: {name}')
    if any(p.lower() in ('logs', 'saves', 'node_modules') for p in path.parts):
        raise ValueError(f'Local data must not be included in an update: {name}')
    if path.name.lower().endswith(('.rzdata', '.log', '.lock')) or path.name.startswith('.'):
        raise ValueError(f'Local file included in update: {name}')
    for part in path.parts:
        if ':' in part or any(c in part for c in '<>\"|?*') or part.endswith((' ', '.')) or part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
            raise ValueError(f'Invalid cross-platform path: {name}')
    return path


def destination(root, name):
    path = root.joinpath(*safe_name(name).parts)
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError(f'Symbolic link in installation path: {name}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Installation path leaves the game directory.')
    if path.exists() and not path.is_file():
        raise ValueError(f'Expected a file but found a directory: {name}')
    return path


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def game_files(files, read=None, declared=None):
    configured = CONFIG.get('game_files', {}).get(PLATFORM)
    if configured and configured != 'auto':
        if not isinstance(configured, list) or not configured:
            raise ValueError('game_files must be auto or a list of filenames.')
        for name in configured:
            safe_name(name)
        return configured
    if declared is not None:
        if not isinstance(declared, list) or not declared:
            raise ValueError('The manifest has no game executable.')
        for name in declared:
            safe_name(name)
        return declared
    names = sorted(name for name in files if '/' not in name)
    if PLATFORM == 'windows':
        candidates = sorted((name for name in names if name.lower().endswith('.exe')),
                            key=lambda name: (name.lower() != 'game.exe', name.lower()))
        if read:
            def pe(name):
                data = read(name)
                offset = int.from_bytes(data[60:64], 'little')
                return data[:2] == b'MZ' and data[offset:offset + 4] == b'PE\0\0'
            candidates = [name for name in candidates if pe(name)]
    else:
        candidates = sorted((name for name in names if name.lower().endswith('.appimage') or name in ('Game', 'Game_Linux', 'mkxp-z', 'mkxp-z.x86_64', 'mkxp')),
                            key=lambda name: (not name.lower().endswith('.appimage'), name != 'Game', name))
        if read:
            candidates = [name for name in candidates if read(name)[:4] == b'\x7fELF']
    if not candidates:
        raise ValueError('No matching game runtime found (Windows: EXE; Linux: AppImage or ELF).')
    return [candidates[0]]


def validate_manifest(manifest, expected_version):
    if manifest.get('format') not in (1, 2) or manifest.get('repo') != REPO or manifest.get('version') != expected_version:
        raise ValueError('The package does not match the selected release.')
    if manifest.get('platform', 'linux') != PLATFORM:
        raise ValueError('The package targets a different operating system.')
    files = manifest['files']
    if not isinstance(files, dict):
        raise ValueError('Invalid file list.')
    required = set(game_files(files, declared=manifest.get('game_files'))) | {'update-version.json'}
    if any(name.startswith('Plugins/') and name.endswith('.rb') for name in files):
        required.add('Data/PluginScripts.rxdata')
    if not isinstance(files, dict) or not required.issubset(files):
        raise ValueError('Incomplete game package.')
    if len({name.casefold() for name in files}) != len(files) or 'update-manifest.json' in files:
        raise ValueError('Conflicting filenames in package.')
    total = 0
    for name, info in files.items():
        safe_name(name)
        if type(info.get('size')) is not int or info['size'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', info.get('sha256', '')):
            raise ValueError(f'Invalid file metadata: {name}')
        total += info['size']
    if total > MAX_BYTES:
        raise ValueError('Update package is too large.')


def protected_files(root):
    protected = {'mkxp.json'}
    if CONFIG_FILE and CONFIG_FILE.is_relative_to(root.resolve()):
        protected.add(CONFIG_FILE.relative_to(root.resolve()).as_posix())
    return protected


def changed_files(root, manifest):
    changed = set()
    for name, info in manifest['files'].items():
        target = destination(root, name)
        if name in protected_files(root) and target.exists():
            continue
        if not target.exists() or target.stat().st_size != info['size'] or sha(target) != info['sha256']:
            changed.add(name)
        elif os.name != 'nt' and bool(target.stat().st_mode & 0o111) != bool(info.get('executable')):
            changed.add(name)
    return changed


class NoAssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def asset_source(info):
    headers = {'User-Agent': 'Game-Patcher', 'Accept-Encoding': 'identity'}
    if not shutil.which('gh'):
        return info['download_url'], headers
    token = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'], capture_output=True, text=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if token.returncode:
        raise RuntimeError('GitHub authentication is missing. Sign in with gh auth login.')
    headers.update({'Accept': 'application/octet-stream', 'Authorization': 'Bearer ' + token.stdout.strip()})
    url = f'https://api.github.com/repos/{REPO}/releases/assets/{info["asset_id"]}?update={time.time_ns()}'
    request = urllib.request.Request(url, headers={**headers, 'Range': 'bytes=0-0'})
    try:
        with urllib.request.build_opener(NoAssetRedirect()).open(request, timeout=60) as response:
            if response.status != 206:
                raise RuntimeError('GitHub does not support partial downloads for this package.')
    except urllib.error.HTTPError as exc:
        if exc.code != 302:
            raise RuntimeError(f'GitHub download is unavailable (HTTP {exc.code}).') from None
        url = exc.headers.get('Location', '')
        exc.close()
        if not url.startswith('https://'):
            raise ValueError('Unsafe GitHub download URL.')
        headers.pop('Authorization')
    return url, headers


def fetch_file(source, info, target, archive_size):
    offset, length = info['offset'], info['compressed_size']
    request = urllib.request.Request(source[0], headers={**source[1], 'Range': f'bytes={offset}-{offset + length - 1}'})
    target.parent.mkdir(parents=True, exist_ok=True)
    decoder = zlib.decompressobj(-15) if info['compression'] == zipfile.ZIP_DEFLATED else None
    with urllib.request.urlopen(request, timeout=60) as response, open(target, 'wb') as output:
        expected = f'bytes {offset}-{offset + length - 1}/{archive_size}'
        if response.status != 206 or response.headers.get('Content-Range') != expected:
            raise RuntimeError('The server did not return a valid partial download. A full download was prevented.')
        received = written = 0
        digest = hashlib.sha256()
        while block := response.read(min(65536, length - received + 1)):
            received += len(block)
            if received > length:
                raise ValueError('Partial download is too large.')
            data = decoder.decompress(block, info['size'] - written + 1) if decoder else block
            written += len(data)
            if written > info['size']:
                raise ValueError('Decompressed file is too large.')
            digest.update(data)
            output.write(data)
        if received != length or written != info['size'] or digest.hexdigest() != info['sha256']:
            raise ValueError('Partial download is incomplete or its SHA256 checksum is incorrect.')
        if decoder and (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError('Invalid compressed file.')
    target.chmod(0o755 if info.get('executable') else 0o644)


def incremental_stage(root, stage, manifest, info):
    validate_manifest(manifest, info['version'])
    archive = manifest.get('archive', {})
    if manifest['format'] != 2 or archive != {'name': ASSET, 'size': info['size'], 'sha256': info['digest'][7:]}:
        raise ValueError('The download index does not match the release package.')
    for name, entry in manifest['files'].items():
        offset, size = entry.get('offset'), entry.get('compressed_size')
        if (type(offset) is not int or type(size) is not int or offset < 0 or size < 0
                or offset + size > info['size'] or entry.get('compression') not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise ValueError(f'Invalid download range: {name}')
    print('Comparing local files ...', flush=True)
    changed = changed_files(root, manifest)
    downloads = []
    for name in sorted(changed):
        entry = manifest['files'][name]
        target = destination(stage, name)
        local = destination(root, name)
        if local.exists() and sha(local) == entry['sha256']:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)
            target.chmod(0o755 if entry.get('executable') else 0o644)
        elif entry['compressed_size'] == 0:
            if entry['size'] != 0 or entry['sha256'] != hashlib.sha256(b'').hexdigest():
                raise ValueError('Invalid empty file.')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'')
            target.chmod(0o755 if entry.get('executable') else 0o644)
        else:
            downloads.append((entry, target))
    total = sum(entry['compressed_size'] for entry, _ in downloads)
    print(f'{len(changed)} new/changed files; download: {total / 1024**2:.2f} MB instead of {info["size"] / 1024**2:.2f} MB.', flush=True)
    if downloads:
        source = asset_source(info)
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tasks = {pool.submit(fetch_file, source, entry, target, info['size']): entry['compressed_size'] for entry, target in downloads}
            for task in concurrent.futures.as_completed(tasks):
                task.result()
                done += tasks[task]
                print(f'\rDownload: {done / 1024**2:.2f} / {total / 1024**2:.2f} MB', end='', flush=True)
        print()
    version_file = stage / 'update-version.json' if 'update-version.json' in changed else root / 'update-version.json'
    if read_json(version_file)['version'] != info['version']:
        raise ValueError('The package version does not match the manifest.')
    return manifest


def unpack(archive, stage, expected_version):
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [i.filename for i in entries]
        if len(names) != len(set(names)) or 'update-manifest.json' not in names:
            raise ValueError('Missing manifest or duplicate package entries.')
        if sum(i.file_size for i in entries) > MAX_BYTES:
            raise ValueError('Update package is too large.')
        if package.getinfo('update-manifest.json').file_size > 16 * 1024**2:
            raise ValueError('Manifest is too large.')
        manifest = json.loads(package.read('update-manifest.json'))
        validate_manifest(manifest, expected_version)
        files = manifest['files']
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError('Filenames conflict on Windows.')
        if set(names) != set(files) | {'update-manifest.json'}:
            raise ValueError('The package and manifest contain different files.')
        for name, info in files.items():
            safe_name(name)
            entry = package.getinfo(name)
            if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Links and directory entries are not allowed in the package.')
            if entry.file_size != info['size'] or not re.fullmatch(r'[0-9a-f]{64}', info['sha256']):
                raise ValueError(f'Invalid file metadata: {name}')
            target = destination(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(entry) as source, open(target, 'wb') as output:
                shutil.copyfileobj(source, output)
            if sha(target) != info['sha256']:
                raise ValueError(f'Checksum mismatch: {name}')
            target.chmod(0o755 if info.get('executable') else 0o644)
        if read_json(stage / 'update-version.json')['version'] != expected_version:
            raise ValueError('The package version does not match the manifest.')
        return manifest


def replace_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix='.update-', dir=target.parent)
    os.close(handle)
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def apply(root, stage, manifest, backup):
    """Preserve settings/saves; restore every touched file if an operation fails."""
    previous = read_json(root / STATE) if (root / STATE).exists() else {'files': {}}
    old_files = previous['files']
    protected = protected_files(root)
    new_files = manifest['files']
    affected = changed_files(root, manifest) | (set(old_files) - set(new_files) - protected)
    targets = {name: destination(root, name) for name in affected}
    for name, target in targets.items():
        if old_files and name not in old_files and target.exists() and name in new_files and sha(target) != new_files[name]['sha256']:
            raise RuntimeError(f'A local file would be overwritten: {name}. Update cancelled.')
        if name in old_files and target.exists() and sha(target) != old_files[name]['sha256']:
            raise RuntimeError(f'Locally modified: {name}. Back up your changes first; update cancelled.')
    if not old_files and (root / '.git').exists():
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=root, capture_output=True, text=True, check=True)
        if status.stdout.strip():
            raise RuntimeError('Commit or back up local project changes first; update cancelled.')
    required = sum(p.stat().st_size for p in targets.values() if p.exists()) + sum(new_files[name]['size'] for name in affected if name in new_files)
    if shutil.disk_usage(root).free < required + 64 * 1024**2:
        raise RuntimeError('Not enough free space for the update and backup.')
    existed = set()
    for name, target in targets.items():
        if target.exists():
            dest = backup / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            existed.add(name)
    old_state = (root / STATE).read_bytes() if (root / STATE).exists() else None
    touched = []
    try:
        total = len(affected)
        for count, name in enumerate(sorted(affected), 1):
            target = targets[name]
            touched.append(name)
            if name in new_files:
                replace_file(stage / name, target)
            else:
                target.unlink(missing_ok=True)
            if count % 500 == 0 or count == total:
                print(f'Installation: {count}/{total} files', flush=True)
        state_temp = stage / 'installed-state.json'
        state_temp.write_text(json.dumps(manifest))
        replace_file(state_temp, root / STATE)
    except BaseException:
        print('Installation failed. Restoring the previous version.', flush=True)
        for name in reversed(touched):
            target = root / name
            if name in existed:
                replace_file(backup / name, target)
            else:
                target.unlink(missing_ok=True)
        if old_state is None:
            (root / STATE).unlink(missing_ok=True)
        else:
            (root / STATE).write_bytes(old_state)
        raise


def wait_for_game(pid):
    if not pid:
        return
    print('Waiting for the game to close ...', flush=True)
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return
            raise RuntimeError('Cannot check the game process.')
        try:
            if kernel.WaitForSingleObject(handle, 60000) == 0:
                return
            raise RuntimeError('The game is still running.')
        finally:
            kernel.CloseHandle(handle)
    for _ in range(120):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    raise RuntimeError('The game is still running. Close it and try again.')


def download(info, target):
    if not 0 < info['size'] <= MAX_BYTES or not re.fullmatch(r'sha256:[0-9a-f]{64}', info['digest']):
        raise ValueError('Invalid download size or checksum.')
    print(f'Download: {info["version"]} ({info["size"] / 1024**2:.1f} MB)', flush=True)
    if shutil.which('gh'):
        with open(target, 'wb') as output:
            with subprocess.Popen(['gh', 'api', '-H', 'Accept: application/octet-stream',
                                   f'repos/{REPO}/releases/assets/{info["asset_id"]}'], stdout=output) as process:
                started = time.monotonic()
                while process.poll() is None:
                    size = target.stat().st_size
                    print(f'\rDownload: {size / 1024**2:.1f} / {info["size"] / 1024**2:.1f} MB', end='', flush=True)
                    if size > MAX_BYTES or time.monotonic() - started > 1800:
                        process.kill()
                        process.wait()
                        raise RuntimeError('Download cancelled: size or time limit exceeded.')
                    time.sleep(1)
                if process.returncode:
                    raise RuntimeError('GitHub download failed. Check your login and connection.')
            print()
    else:
        req = urllib.request.Request(info['download_url'], headers={'User-Agent': 'Game-Patcher'})
        with urllib.request.urlopen(req, timeout=60) as source, open(target, 'wb') as output:
            done = 0
            while block := source.read(1024 * 1024):
                done += len(block)
                if done > MAX_BYTES:
                    raise ValueError('Download is too large.')
                output.write(block)
                print(f'\rDownload: {done / 1024**2:.1f} MB', end='', flush=True)
        print()
    if target.stat().st_size != info['size'] or 'sha256:' + sha(target) != info['digest']:
        raise ValueError('Download is incomplete or its SHA256 checksum is incorrect.')


def install(root, pid):
    with install_lock(root):
        wait_for_game(pid)
        print('Checking GitHub releases ...', flush=True)
        info = check(root)
        if info['status'] != 'available':
            print(f'Already up to date ({info["installed"]}). {info.get("reason", "")}', flush=True)
            return
        base = Path(os.environ.get('LOCALAPPDATA' if os.name == 'nt' else 'XDG_DATA_HOME', str(Path.home() / '.local/share'))) / CONFIG.get('backup_folder', 'GamePatcher') / 'update-backups'
        base.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=base))
        with tempfile.TemporaryDirectory(prefix='game-update-', dir=root.parent) as temp:
            temp = Path(temp)
            stage = temp / 'stage'
            stage.mkdir()
            if 'manifest' in info:
                if not 0 < info['manifest']['size'] <= 16 * 1024**2:
                    raise ValueError('Download index is too large.')
                index = temp / 'manifest.json'
                download(info['manifest'], index)
                manifest = incremental_stage(root, stage, read_json(index), info)
            else:
                print('This older release has no download index; downloading the full package once.', flush=True)
                archive = temp / ASSET
                download(info, archive)
                print('Verifying and extracting package ...', flush=True)
                manifest = unpack(archive, stage, info['version'])
            print(f'Backup: {backup}', flush=True)
            apply(root, stage, manifest, backup)
        print(f'Update to {info["version"]} complete. Restart the game.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['check', 'install', 'launch'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--wait-pid', type=int)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        config_path = (args.config or Path(__file__).resolve().parents[1] / 'settings.json').resolve()
        configure(root, config_path)
        if args.command == 'launch':
            if os.name != 'nt':
                raise RuntimeError('launch is only available on Windows.')
            launch_windows(root, args.wait_pid, config_path)
            return 0
        if args.command == 'check':
            print(json.dumps(check(root), ensure_ascii=False))
        else:
            title = CONFIG.get('title', 'Game Patcher')
            if os.name == 'nt':
                ctypes.windll.kernel32.SetConsoleTitleW(title + ' – Update')
            print(title + ' – Release Update', flush=True)
            install(root, args.wait_pid)
        return 0
    except Exception as exc:
        if args.command == 'check':
            print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False))
        else:
            print(f'ERROR: {exc}', file=sys.stderr, flush=True)
            print('The update was not completed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    code = main()
    if len(sys.argv) > 1 and sys.argv[1] == 'install' and sys.stdin.isatty():
        try:
            input('\nPress Enter to close the terminal ... ')
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
