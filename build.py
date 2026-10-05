#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2025 The Helium Authors
# You can use, redistribute, and/or modify this source code under
# the terms of the GPL-3.0 license that can be found in the LICENSE file.

# Copyright (c) 2019 The ungoogled-chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""
Helium build script for Windows
"""

import sys
import time
import json
import atexit
import hashlib
import argparse
import os
import shutil
import stat
import subprocess
import ctypes
from pathlib import Path
from contextlib import chdir

sys.path.insert(0, str(Path(__file__).resolve().parent / 'helium-chromium' / 'utils'))
import downloads
import domain_substitution
import i18n_apply
import name_substitution
import helium_version
import generate_resources
import replace_resources
import prune_binaries
import patches
from _common import ENCODING, USE_REGISTRY, ExtractorEnum, get_logger
sys.path.pop(0)

_ROOT_DIR = Path(__file__).resolve().parent
_PATCH_BIN_RELPATH = Path('third_party/git/usr/bin/patch.exe')


def _rmtree_clear_readonly(func, path, exc_info):
    """shutil.rmtree onexc handler: CIPD/npm mark some installed files
    read-only on Windows, which makes os.unlink/os.rmdir fail. Clear the
    read-only attribute and retry once."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path):
    shutil.rmtree(path, onexc=_rmtree_clear_readonly)


def _clear_readonly_tree(path):
    """CIPD/npm mark some installed files read-only on Windows, which makes
    `git clean` (run by clone.py) fail with 'Directory not empty' instead of
    actually removing them. Proactively clear the read-only attribute across
    the whole tree before clean runs, so it can delete everything itself.
    Best-effort: a file this account has no access to at all (not just
    read-only) is logged and left for the normal error to surface."""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            entry = Path(root) / name
            try:
                os.chmod(entry, stat.S_IWRITE)
            except OSError as exc:
                get_logger().warning('Could not clear read-only attribute on %s: %s', entry, exc)


# rust-toolchain and llvm-build (clang) each carry their own version stamp
# and skip re-downloading (multiple GB) when it already matches -- but
# clone.py's `git reset --hard` + `git clean -ffdx` wipes them every build,
# since they're untracked DEPS output, not excluded like uc_staging is.
_TOOLCHAIN_CACHE_DIRS = (
    Path('third_party/rust-toolchain'),
    Path('third_party/llvm-build/Release+Asserts'),
)


def _stash_toolchain_caches(source_tree, cache_root):
    """Move the toolchain directories out of source_tree before clone.py's
    git clean runs, so it can't touch them."""
    for rel_dir in _TOOLCHAIN_CACHE_DIRS:
        src = source_tree / rel_dir
        if not src.exists():
            continue
        dest = cache_root / rel_dir
        if dest.exists():
            rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def _restore_toolchain_caches(source_tree, cache_root):
    """Move the toolchain directories back after clone.py finishes, before
    update_rust.py/update.py run. If the toolchain revision Chromium's DEPS
    asks for hasn't changed, their own stamp check skips the download; if it
    has, they detect the mismatch and re-download as normal."""
    for rel_dir in _TOOLCHAIN_CACHE_DIRS:
        src = cache_root / rel_dir
        if not src.exists():
            continue
        dest = source_tree / rel_dir
        if dest.exists():
            rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


_PREPARE_FINGERPRINT_INPUTS = (
    Path('helium-chromium/chromium_version.txt'),
    Path('helium-chromium/pruning.list'),
    Path('helium-chromium/domain_substitution.list'),
    Path('helium-chromium/domain_regex.list'),
    Path('helium-chromium/deps.ini'),
    Path('helium-chromium/downloads.ini'),
    Path('helium-chromium/flags.gn'),
    Path('helium-chromium/resources/generate_resources.txt'),
    Path('helium-chromium/resources/helium_resources.txt'),
    Path('downloads.ini'),
    Path('flags.windows.gn'),
    Path('resources/generate_resources.txt'),
    Path('resources/platform_resources.txt'),
)


def _compute_prepare_fingerprint(root_dir, args):
    """Hash everything that feeds into the clone/download/patch/prune/
    substitution steps below, so a local (non-CI) run can tell whether that
    whole multi-minute (and mtime-touching, incremental-build-defeating)
    sequence actually needs to happen again, or whether the last prepared
    source_tree is still current.

    Deliberately narrow: it covers patches/series (and every patch file they
    list, in order) plus the small set of list/ini/gn files that drive
    pruning, domain/name substitution and resource generation. It does not
    walk the full resources/ directory tree, so a change to a resource file
    that isn't also reflected in one of the *_resources.txt manifests won't
    be picked up -- edit chromium_version.txt (or delete build/src) to force
    a re-prepare if that ever matters.
    """
    import hashlib
    hasher = hashlib.sha256()

    def add_bytes(data):
        hasher.update(len(data).to_bytes(8, 'little'))
        hasher.update(data)

    def add_file(path):
        add_bytes(path.read_bytes() if path.exists() else b'<missing>')

    # Deliberately excludes args.dev: --dev only changes GN args (component
    # build, PGO, optimize_webui) and whether domain/name substitution + i18n
    # run, not the clone/patch/prune steps themselves. A tree already fully
    # prepared by a normal run is perfectly valid to reuse for a --dev run
    # (see the separate "tree_has_substitution" check in main() for the one
    # case -- a dev-raw tree being mistaken for a fully substituted one --
    # this fingerprint alone can't catch).
    add_bytes(f'arm={args.arm};tarball={args.tarball}'.encode(ENCODING))

    for series_dir in (root_dir / 'helium-chromium' / 'patches', root_dir / 'patches'):
        add_file(series_dir / 'series')
        for patch_path in patches.generate_patches_from_series(series_dir, resolve=True):
            add_file(patch_path)

    for rel_path in _PREPARE_FINGERPRINT_INPUTS:
        add_file(root_dir / rel_path)

    return hasher.hexdigest()


def _compute_inputs_fingerprint(root_dir, args):
    """Like _compute_prepare_fingerprint(), minus the patches: everything else
    that feeds the prepare. _try_incremental_patch_update() may only edit the
    patched tree in place when this is unchanged (same Chromium version, same
    pruning/substitution lists, ...) and only the patches differ."""
    hasher = hashlib.sha256()

    def add(data):
        hasher.update(len(data).to_bytes(8, 'little'))
        hasher.update(data)

    add(f'arm={args.arm};tarball={args.tarball}'.encode(ENCODING))
    for rel_path in _PREPARE_FINGERPRINT_INPUTS:
        path = root_dir / rel_path
        add(path.read_bytes() if path.exists() else b'<missing>')
    return hasher.hexdigest()


def _patch_entries(root_dir):
    """Every patch of both series in the order build.py applies them."""
    entries = []
    for label, series_dir in (('chromium', root_dir / 'helium-chromium' / 'patches'),
                              ('windows', root_dir / 'patches')):
        for rel in patches.parse_series(series_dir / 'series'):
            path = (series_dir / rel).resolve()
            entries.append({
                'label': label,
                'rel': str(rel).replace('\\', '/'),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'path': path,
            })
    return entries


_APPLIED_PATCHES_DIR = 'applied_patches'
_APPLIED_PATCHES_RECORD = 'applied_patches.json'
# Beyond this many patches (an upstream sync, say) a clean prepare is the safer bet.
_MAX_INCREMENTAL_PATCHES = 60


def _save_applied_patches(cache_dir, entries):
    """Keep a copy of the patches the tree was prepared with: undoing them later
    needs the exact text that was applied, not whatever the files say by then."""
    target = cache_dir / _APPLIED_PATCHES_DIR
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    record = []
    for index, entry in enumerate(entries):
        saved = f'{index:04d}.patch'
        shutil.copyfile(entry['path'], target / saved)
        record.append({k: entry[k] for k in ('label', 'rel', 'sha256')} | {'file': saved})
    # Written last: a record without its files (interrupted copy) is rejected on load.
    (cache_dir / _APPLIED_PATCHES_RECORD).write_text(json.dumps(record), encoding=ENCODING)


def _load_applied_patches(cache_dir):
    """The saved record, or None if it is missing or does not match its files."""
    try:
        record = json.loads((cache_dir / _APPLIED_PATCHES_RECORD).read_text(encoding=ENCODING))
        for entry in record:
            data = (cache_dir / _APPLIED_PATCHES_DIR / entry['file']).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record


def _backfill_incremental_state(root_dir, cache_dir, fingerprint_path, marker_lines,
                                inputs_fingerprint):
    """A tree whose fingerprint still matches was prepared with exactly today's
    patch files, so the record that makes in-place updates possible can be
    created from them (trees prepared before this existed have none)."""
    has_inputs = len(marker_lines) > 2 and marker_lines[2].startswith('inputs=')
    if has_inputs and _load_applied_patches(cache_dir) is not None:
        return
    _save_applied_patches(cache_dir, _patch_entries(root_dir))
    lines = list(marker_lines[:2]) + [f'inputs={inputs_fingerprint}']
    fingerprint_path.write_text('\n'.join(lines) + '\n', encoding=ENCODING)


def _try_incremental_patch_update(root_dir, args, source_tree, cache_dir, fingerprint_path,
                                  marker_lines, current_fingerprint, inputs_fingerprint,
                                  patch_bin):
    """Bring a --dev tree up to date with changed patches without re-preparing it.

    Re-preparing means a fresh clone, so the mtime of nearly every file changes
    and Siso rebuilds almost everything; undoing only the patches from the first
    changed one onwards and applying the new ones touches just the files those
    patches are about. Returns True when the tree is current afterwards. False
    means nothing here was usable (or a patch did not undo/apply cleanly, which
    can leave the tree half-patched) and the caller must prepare from scratch;
    that is also what happens if this is interrupted, because the fingerprint
    file is invalidated before the tree is touched.

    Only valid for a --dev tree: a normal prepare substitutes domains and names
    in the files the patches touched, so its patches could not be reversed.
    """
    logger = get_logger()
    if not (args.dev and len(marker_lines) > 2 and marker_lines[1] == 'dev=True'
            and marker_lines[2] == f'inputs={inputs_fingerprint}'):
        return False
    if not (source_tree / 'BUILD.gn').exists():
        return False
    old = _load_applied_patches(cache_dir)
    if old is None:
        return False

    new = _patch_entries(root_dir)

    def key(entry):
        return (entry['label'], entry['rel'], entry['sha256'])

    common = 0
    while common < min(len(old), len(new)) and key(old[common]) == key(new[common]):
        common += 1
    old_tail, new_tail = old[common:], new[common:]
    if max(len(old_tail), len(new_tail)) > _MAX_INCREMENTAL_PATCHES:
        logger.info('Too many patches changed (%d/%d) for an in-place update.',
                    len(old_tail), len(new_tail))
        return False

    logger.info('Patches changed: undoing %d and applying %d in place (the first %d are '
                'unchanged).', len(old_tail), len(new_tail), common)
    fingerprint_path.write_text('incremental-patch-update-in-progress\n', encoding=ENCODING)
    try:
        if old_tail:
            patches.apply_patches(
                [cache_dir / _APPLIED_PATCHES_DIR / entry['file'] for entry in old_tail],
                source_tree, reverse=True, patch_bin_path=patch_bin)
        if new_tail:
            patches.apply_patches([entry['path'] for entry in new_tail], source_tree,
                                  patch_bin_path=patch_bin)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        logger.warning('Updating the patches in place failed (%s); preparing from scratch.', exc)
        return False

    _save_applied_patches(cache_dir, new)
    fingerprint_path.write_text(
        f'{current_fingerprint}\ndev=True\ninputs={inputs_fingerprint}\n', encoding=ENCODING)
    return True


class _PrepareProgress:
    """Local-only record of how far the prepare block got (build/download_cache/
    prepare_progress.json), with a numbered log line per step.

    It does NOT make prepare resumable: unpacking an archive into an already
    populated tree fails on the final rename, pruning an already pruned tree
    reports every file as missing, and a half-applied patch leaves the tree
    inconsistent, so a prepare that stopped midway has to start from the clone
    again. What this gives you is knowing where it stopped, immediately when it
    happens and again at the start of the next run.
    """

    TOTAL = 10

    def __init__(self, path, enabled):
        self.path = path
        self.enabled = enabled
        self.index = 0
        self.name = None
        self.finished = False
        self.step_started = self.started = time.time()
        if not enabled:
            return
        try:
            previous = json.loads(path.read_text(encoding=ENCODING))
        except (OSError, ValueError):
            previous = {}
        if previous.get('state') == 'running':
            get_logger().warning(
                'The previous prepare stopped during step %s/%s "%s". Steps cannot safely '
                'resume midway, so the source tree is prepared from scratch again.',
                previous.get('index'), self.TOTAL, previous.get('step'))
        atexit.register(self._report_stop)

    def _write(self, state):
        try:
            self.path.write_text(json.dumps({
                'state': state, 'index': self.index, 'step': self.name,
                'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
            }), encoding=ENCODING)
        except OSError:
            pass

    def step(self, name):
        if not self.enabled:
            return
        now = time.time()
        if self.name:
            get_logger().info('    done in %.0fs', now - self.step_started)
        self.index += 1
        self.name = name
        self.step_started = now
        get_logger().info('[%d/%d] %s', self.index, self.TOTAL, name)
        self._write('running')

    def done(self):
        if not self.enabled:
            return
        self.finished = True
        get_logger().info('Prepare finished in %.0fs.', time.time() - self.started)
        self._write('done')

    def _report_stop(self):
        if self.enabled and self.name and not self.finished:
            get_logger().error('Prepare stopped during step %d/%d "%s" after %.0fs.',
                               self.index, self.TOTAL, self.name,
                               time.time() - self.step_started)


def _check_downloads_cached(download_info, cache_dir, components, enabled):
    """downloads.check_downloads(), but a local run skips the files it already
    verified: same size, same modification time and the same expected hashes as
    when they last passed (build/download_cache/verified_downloads.json). Hashing
    the Chromium archive alone takes about half a minute on every run."""
    if not enabled:
        return downloads.check_downloads(download_info, cache_dir, components)

    record_path = cache_dir / 'verified_downloads.json'
    try:
        record = json.loads(record_path.read_text(encoding=ENCODING))
    except (OSError, ValueError):
        record = {}

    def fingerprint(name, props):
        stat = (cache_dir / props.download_filename).stat()
        hashes = sorted(str(pair) for pair in downloads._get_hash_pairs(props, cache_dir))
        digest = hashlib.sha256(repr(hashes).encode(ENCODING)).hexdigest()
        return [stat.st_size, stat.st_mtime_ns, digest]

    pending, stamps = [], {}
    for name, props in download_info.properties_iter():
        if components and name not in components:
            continue
        try:
            stamps[name] = fingerprint(name, props)
        except OSError:
            pending.append(name)  # missing file: let check_downloads report it
            continue
        if record.get(props.download_filename) != stamps[name]:
            pending.append(name)

    skipped = len(stamps) - len([n for n in pending if n in stamps])
    if skipped:
        get_logger().info('Skipping hash verification of %d already verified download(s)', skipped)
    if pending:
        downloads.check_downloads(download_info, cache_dir, pending)
        for name in pending:
            props = download_info[name]
            try:
                record[props.download_filename] = fingerprint(name, props)
            except OSError:
                pass
        try:
            record_path.write_text(json.dumps(record), encoding=ENCODING)
        except OSError:
            pass


def _clear_stale_extraction_staging(download_info, components, output_dir):
    """Extractors that can't tell 7z to strip the archive's leading directory
    themselves unpack into a temporary `<output_path>/<strip_leading_dirs>`
    staging directory, then move its contents up into <output_path> one file
    at a time and remove it. If a previous build was interrupted mid-move,
    the staging directory can be left behind with only some of its files
    moved out -- and since <output_path> then already has the rest, even a
    fresh extraction attempt fails the same way, file by file. This is all
    in DEPS-downloaded, git-untracked third_party paths that `git clean`
    never touches, so it doesn't get cleared on its own.

    Clear the whole <output_path> destination (not just the staging
    directory) before extracting, so every attempt starts from an empty
    directory and can't collide with anything. These are small,
    quick-to-re-extract packages (headers, small tools), so re-extracting
    them unconditionally on every build is cheap."""
    for download_name, download_properties in download_info.properties_iter():
        if components and download_name not in components:
            continue
        if download_properties.strip_leading_dirs is None:
            continue
        destination = output_dir / Path(download_properties.output_path)
        if destination.exists():
            get_logger().warning('Clearing extraction destination before unpacking: %s',
                                 destination)
            rmtree(destination)
            destination.mkdir(parents=True)


def _unpack_downloads_resilient(download_info, cache_dir, components, output_dir, extractors,
                                max_attempts=10):
    """Stale extraction leftovers from an interrupted previous build (in
    DEPS-downloaded third_party directories that `git clean` never touches,
    since they aren't tracked by git) can make extraction fail with
    FileExistsError, either because a leftover staging directory blocks a
    fresh extraction (see _clear_stale_extraction_staging) or because moving
    a file out of that staging directory collides with one already at the
    destination. Clear whatever's blocking it and retry."""
    for attempt in range(1, max_attempts + 1):
        # A prior attempt in this same retry loop can die partway through
        # _process_relative_to(), leaving that component's staging directory
        # behind with only some of its files moved out. Since the next
        # attempt reprocesses every component from scratch, that leftover
        # staging directory trips the "already exists" precheck even though
        # nothing outside this loop is stale. Clear it before every attempt,
        # not just the first.
        try:
            _clear_stale_extraction_staging(download_info, components, output_dir)
            downloads.unpack_downloads(download_info, cache_dir, components, output_dir,
                                       extractors)
            return
        except PermissionError as exc:
            # Windows refuses to rename/delete a directory while another process holds a
            # handle inside it. Right after extraction that is almost always a real-time
            # antivirus/EDR scan (e.g. Trellix) or the search indexer still reading the
            # freshly written files; it clears up on its own after a few seconds.
            if attempt == max_attempts:
                raise
            delay = 5 * attempt
            get_logger().warning(
                'Access denied during extraction (%s); a scanner probably has the files open. '
                'Waiting %ds and retrying (attempt %d/%d).', exc.filename or exc, delay,
                attempt, max_attempts)
            time.sleep(delay)
        except FileExistsError as exc:
            if attempt == max_attempts:
                raise
            blocking_path = exc.filename2 or exc.filename
            if blocking_path:
                get_logger().warning(
                    'Extraction hit a stale leftover at %s (attempt %d/%d); removing it and '
                    'retrying.', blocking_path, attempt, max_attempts)
                blocking_path = Path(blocking_path)
                if blocking_path.is_dir():
                    rmtree(blocking_path)
                else:
                    blocking_path.unlink(missing_ok=True)
            else:
                # A bare FileExistsError (no filename attached) is the
                # extractors' own "temporary unpacking directory already
                # exists" precheck; _clear_stale_extraction_staging() at the
                # top of the next attempt clears whatever tripped it.
                get_logger().warning(
                    'Extraction hit a stale unpacking directory (attempt %d/%d); retrying.',
                    attempt, max_attempts)


def _get_vcvars_path(name='64'):
    """
    Returns the path to the corresponding vcvars*.bat path

    As of VS 2017, name can be one of: 32, 64, all, amd64_x86, x86_amd64
    """
    vswhere_exe = '%ProgramFiles(x86)%\\Microsoft Visual Studio\\Installer\\vswhere.exe'
    result = subprocess.run(
        '"{}" -products * -prerelease -latest -property installationPath'.format(vswhere_exe),
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
        universal_newlines=True)
    vcvars_path = Path(result.stdout.strip(), 'VC/Auxiliary/Build/vcvars{}.bat'.format(name))
    if not vcvars_path.exists():
        raise RuntimeError(
            'Could not find vcvars batch script in expected location: {}'.format(vcvars_path))
    return vcvars_path


def _run_build_process(*args, **kwargs):
    """
    Runs the subprocess with the correct environment variables for building
    """
    # Add call to set VC variables
    cmd_input = ['call "%s" >nul' % _get_vcvars_path()]
    cmd_input.append('set DEPOT_TOOLS_WIN_TOOLCHAIN=0')
    cmd_input.append(' '.join(map('"{}"'.format, args)))
    cmd_input.append('exit\n')
    subprocess.run(('cmd.exe', '/k'),
                   input='\n'.join(cmd_input),
                   check=True,
                   encoding=ENCODING,
                   **kwargs)


def _run_build_process_timeout(*args, timeout):
    """
    Runs the subprocess with the correct environment variables for building
    """
    # Add call to set VC variables
    cmd_input = ['call "%s" >nul' % _get_vcvars_path()]
    cmd_input.append('set DEPOT_TOOLS_WIN_TOOLCHAIN=0')
    cmd_input.append(' '.join(map('"{}"'.format, args)))
    cmd_input.append('exit\n')
    with subprocess.Popen(('cmd.exe', '/k'), encoding=ENCODING, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) as proc:
        proc.stdin.write('\n'.join(cmd_input))
        proc.stdin.close()
        try:
            proc.wait(timeout)
            if proc.returncode != 0:
                raise RuntimeError('Build failed!')
        except subprocess.TimeoutExpired:
            print('Sending keyboard interrupt')
            for _ in range(3):
                ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, proc.pid)
                time.sleep(1)
            try:
                proc.wait(10)
            except:
                proc.kill()
            sys.exit(42)


def _make_tmp_paths():
    """Creates TMP and TEMP variable dirs so ninja won't fail"""
    tmp_path = Path(os.environ['TMP'])
    if not tmp_path.exists():
        tmp_path.mkdir()
    tmp_path = Path(os.environ['TEMP'])
    if not tmp_path.exists():
        tmp_path.mkdir()


def _configure_remoteexec(source_tree):
    address = os.environ.get('SISO_REAPI_ADDRESS', '')
    instance = os.environ.get('SISO_REAPI_INSTANCE') or 'main'
    backend = ''
    if address:
        os.environ['SISO_REAPI_INSTANCE'] = instance
        backend = 'nativelink.star'

    subprocess.run([
        sys.executable, str(source_tree / 'build/config/siso/configure_siso.py'),
        '--rbe_instance=projects/rbe-chrome-untrusted/instances/default_instance',
        f'--reapi_address={address}',
        f'--reapi_instance={instance if address else ""}',
        f'--reapi_backend_config_path={backend}',
    ], check=True)


def main():
    """CLI Entrypoint"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--7z-path',
        dest='sevenz_path',
        default=USE_REGISTRY,
        help=('Command or path to 7-Zip\'s "7z" binary. If "_use_registry" is '
              'specified, determine the path from the registry. Default: %(default)s'))
    parser.add_argument(
        '--winrar-path',
        dest='winrar_path',
        default=USE_REGISTRY,
        help=('Command or path to WinRAR\'s "winrar.exe" binary. If "_use_registry" is '
              'specified, determine the path from the registry. Default: %(default)s'))
    parser.add_argument(
        '-j',
        type=int,
        dest='thread_count',
        help=('Number of CPU threads to use for compiling'))
    parser.add_argument(
        '--ci',
        type=int,
    )
    parser.add_argument(
        '--arm',
        action='store_true'
    )
    parser.add_argument(
        '--tarball',
        action='store_true'
    )
    parser.add_argument(
        '--dev',
        action='store_true'
    )
    parser.add_argument(
        '--installer',
        action='store_true',
        help=('Also build chromedriver, setup and mini_installer. By default (and with '
              '--dev, and in CI) only chrome is built, which is all the portable ZIP '
              'needs. Only changes the ninja targets, so it is not part of the prepare '
              'fingerprint.')
    )
    parser.add_argument(
        '--portable-only',
        action='store_true',
        help=argparse.SUPPRESS  # the default now; still accepted so existing callers work
    )
    args = parser.parse_args()
    args.portable_only = not args.installer

    # Set common variables
    source_tree = _ROOT_DIR / 'build' / 'src'
    downloads_cache = _ROOT_DIR / 'build' / 'download_cache'
    toolchain_cache = _ROOT_DIR / 'build' / 'toolchain_cache'
    if os.environ.get('SISO_REAPI_ADDRESS'):
        os.environ['RBE_service_no_security'] = 'true'

    # Local (non-CI) runs otherwise re-clone/re-patch the whole source tree on
    # every invocation, rewriting the mtime of every patched file even when
    # nothing actually changed -- which makes ninja/Siso treat most of the
    # tree as dirty and rebuild far more than necessary. Skip the prepare
    # block for local runs when BUILD.gn already exists and the fingerprint
    # of everything that feeds it (patches, chromium_version, etc.) matches
    # what produced the current source_tree.
    prepare_fingerprint_path = downloads_cache / 'prepare_fingerprint.txt'
    current_fingerprint = None
    if not args.ci:
        current_fingerprint = _compute_prepare_fingerprint(_ROOT_DIR, args)
        stored_fingerprint = None
        marker_lines = []
        stored_tree_is_dev_raw = True  # unknown marker => assume the worst
        if prepare_fingerprint_path.exists():
            marker_lines = prepare_fingerprint_path.read_text(encoding=ENCODING).splitlines()
            stored_fingerprint = marker_lines[0] if marker_lines else None
            stored_tree_is_dev_raw = len(marker_lines) > 1 and marker_lines[1] == 'dev=True'
        need_prepare = (
            not (source_tree / 'BUILD.gn').exists()
            or current_fingerprint != stored_fingerprint
            # A --dev prepare skips domain/name substitution + i18n for
            # speed, so that tree is missing steps a normal build needs --
            # never silently reuse it for a non-dev run, even though the
            # patch/chromium_version fingerprint alone matches.
            or (not args.dev and stored_tree_is_dev_raw)
        )
    else:
        need_prepare = not (source_tree / 'BUILD.gn').exists()

    # Set when this run changed the tree (full prepare or in-place patch update);
    # decides whether `gn gen` has to run again below.
    tree_changed = need_prepare
    inputs_fingerprint = None
    if not args.ci:
        inputs_fingerprint = _compute_inputs_fingerprint(_ROOT_DIR, args)
        if not need_prepare:
            _backfill_incremental_state(_ROOT_DIR, downloads_cache, prepare_fingerprint_path,
                                        marker_lines, inputs_fingerprint)
        elif _try_incremental_patch_update(
                _ROOT_DIR, args, source_tree, downloads_cache, prepare_fingerprint_path,
                marker_lines, current_fingerprint, inputs_fingerprint,
                source_tree / _PATCH_BIN_RELPATH):
            need_prepare = False
            tree_changed = True

    if need_prepare:
        progress = _PrepareProgress(downloads_cache / 'prepare_progress.json', not args.ci)

        # Setup environment
        source_tree.mkdir(parents=True, exist_ok=True)
        downloads_cache.mkdir(parents=True, exist_ok=True)
        _make_tmp_paths()

        # Extractors
        extractors = {
            ExtractorEnum.SEVENZIP: args.sevenz_path,
            ExtractorEnum.WINRAR: args.winrar_path,
        }

        # Prepare source folder
        progress.step('Fetch the Chromium source (clone or tarball)')
        if args.tarball:
            # Download chromium tarball
            get_logger().info('Downloading chromium tarball...')
            download_info = downloads.DownloadInfo([_ROOT_DIR / 'helium-chromium' / 'downloads.ini'])
            downloads.retrieve_downloads(download_info, downloads_cache, None, True)
            try:
                _check_downloads_cached(download_info, downloads_cache, None, not args.ci)
            except downloads.HashMismatchError as exc:
                get_logger().error('File checksum does not match: %s', exc)
                exit(1)

            # Unpack chromium tarball
            get_logger().info('Unpacking chromium tarball...')
            _unpack_downloads_resilient(download_info, downloads_cache, None, source_tree, extractors)
        else:
            # Clone sources. clone.py runs `git clean -ffdx`, which can fail if
            # CIPD/npm left read-only files behind from a previous run, and
            # would otherwise wipe the rust/clang toolchains every time.
            _clear_readonly_tree(source_tree)
            _stash_toolchain_caches(source_tree, toolchain_cache)
            subprocess.run([sys.executable, str(Path('helium-chromium', 'utils', 'clone.py')), '-o', 'build\\src', '-p', 'win-arm64' if args.arm else 'win64'], check=True)
            _restore_toolchain_caches(source_tree, toolchain_cache)

        # Retrieve windows downloads
        progress.step('Download and verify the required files and deps')
        get_logger().info('Downloading required files...')
        download_info_win = downloads.DownloadInfo([_ROOT_DIR / 'downloads.ini'])
        components = list(download_info_win)
        if not os.environ.get('SISO_REAPI_ADDRESS'):
            components.remove('nodejs-linux')
        downloads.retrieve_downloads(download_info_win, downloads_cache, components, True)
        try:
            _check_downloads_cached(download_info_win, downloads_cache, components, not args.ci)
        except downloads.HashMismatchError as exc:
            get_logger().error('File checksum does not match: %s', exc)
            exit(1)

        # Retrieve deps
        get_logger().info('Downloading deps...')
        deps_info = downloads.DownloadInfo([_ROOT_DIR / 'helium-chromium' / 'deps.ini'])
        downloads.retrieve_downloads(deps_info, downloads_cache, None, True)
        try:
            _check_downloads_cached(deps_info, downloads_cache, None, not args.ci)
        except downloads.HashMismatchError as exc:
            get_logger().error('File checksum does not match: %s', exc)
            exit(1)
        progress.step('Unpack deps')
        get_logger().info('Unpacking deps...')
        _unpack_downloads_resilient(deps_info, downloads_cache, None, source_tree, extractors)


        # Prune binaries
        progress.step('Prune binaries')
        pruning_list = _ROOT_DIR / 'helium-chromium' / 'pruning.list'
        unremovable_files = prune_binaries.prune_files(
            source_tree,
            pruning_list.read_text(encoding=ENCODING).splitlines()
        )
        # The source file lists are validated in CI against the official lite tarball,
        # which ships third_party/chromium-bidi/node_modules; the tree cloned above does
        # not have it, so its entries in pruning.list are legitimately absent here.
        unremovable_files = {f for f in unremovable_files
                             if not f.startswith('third_party/chromium-bidi/node_modules/')}
        if unremovable_files:
            get_logger().error('Files could not be pruned: %s', unremovable_files)
            parser.exit(1)

        # Unpack downloads
        DIRECTX = source_tree / 'third_party' / 'microsoft_dxheaders' / 'src'
        ESBUILD = source_tree / 'third_party' / 'devtools-frontend' / 'src' / 'third_party' / 'esbuild'
        if DIRECTX.exists():
            rmtree(DIRECTX)
            DIRECTX.mkdir()
        if ESBUILD.exists():
            rmtree(ESBUILD)
            ESBUILD.mkdir()
        progress.step('Unpack downloads')
        get_logger().info('Unpacking downloads...')
        _unpack_downloads_resilient(download_info_win, downloads_cache, components, source_tree, extractors)

        progress.step('Install CIPD dependencies')
        cipd_cache = downloads_cache / 'cipd'
        cipd_cache.mkdir(exist_ok=True)
        cipd_env = os.environ.copy()
        cipd_env['CIPD_CACHE_DIR'] = str(cipd_cache)
        cipd_command = [
            sys.executable,
            str(_ROOT_DIR / 'helium-chromium' / 'utils' / 'install_cipd_deps.py'),
            source_tree,
        ]
        if os.environ.get('SISO_REAPI_ADDRESS'):
            cipd_command.append('--remote-exec')
        subprocess.run(cipd_command, check=True, env=cipd_env)

        # clone.py skips the gclient hook that creates the Siso backend config.
        siso_backend_dir = source_tree / 'build/config/siso/backend_config'
        if not (siso_backend_dir / 'backend.star').exists():
            shutil.copyfile(siso_backend_dir / 'google.star',
                            siso_backend_dir / 'backend.star')

        # Apply patches
        progress.step('Apply patches')
        applied_entries = None if args.ci else _patch_entries(_ROOT_DIR)
        # First, ungoogled-chromium-patches
        patches.apply_patches(
            patches.generate_patches_from_series(_ROOT_DIR / 'helium-chromium' / 'patches', resolve=True),
            source_tree,
            patch_bin_path=(source_tree / _PATCH_BIN_RELPATH)
        )
        # Then Windows-specific patches
        patches.apply_patches(
            patches.generate_patches_from_series(_ROOT_DIR / 'patches', resolve=True),
            source_tree,
            patch_bin_path=(source_tree / _PATCH_BIN_RELPATH)
        )

        progress.step('Update the Rust and Clang toolchains')
        # Download toolchains after the Windows extraction patch, before domain
        # substitution rewrites the download URL shared by Rust and Clang.
        with chdir(source_tree):
            _run_build_process(sys.executable, 'tools\\rust\\update_rust.py')
            _run_build_process(sys.executable, 'tools\\clang\\scripts\\update.py')
            if os.environ.get('SISO_REAPI_ADDRESS'):
                _run_build_process(
                    sys.executable, 'tools\\clang\\scripts\\update.py',
                    '--host-os=linux',
                    '--output-dir=third_party/llvm-build/Release+Asserts_linux')

        progress.step('Substitute domains and names, add translations')
        if not args.dev:
            # Substitute domains
            domain_substitution_list = _ROOT_DIR / 'helium-chromium' / 'domain_substitution.list'
            domain_substitution.apply_substitution(
                _ROOT_DIR / 'helium-chromium' / 'domain_regex.list',
                domain_substitution_list,
                source_tree,
                None
            )

            # Substitute names
            name_substitution.do_substitution(
                source_tree,
                tarpath=None,
                workers=min(32, os.cpu_count()),
                dry_run=False
            )

            # Append translations
            i18n_apply.apply_translations(source_tree)

        progress.step('Set the version and copy resources')
        # Set version
        version_parts = helium_version.get_version_parts(_ROOT_DIR / 'helium-chromium', _ROOT_DIR)
        chrome_version_path = source_tree / "chrome" / "VERSION"
        helium_version.check_existing_version(chrome_version_path)
        with open(chrome_version_path, "a") as f:
            for name, version in version_parts.items():
                helium_version.append_version(f, name, version)

        # Copy resources
        # First, generate and copy Windows-specific resources
        generate_resources.generate_resources(
            _ROOT_DIR / 'resources' / 'generate_resources.txt',
            _ROOT_DIR / 'resources'
        )

        replace_resources.copy_resources(
            _ROOT_DIR / 'resources' / 'platform_resources.txt',
            _ROOT_DIR / 'resources',
            source_tree
        )

        # Then common helium-chromium resources
        generate_resources.generate_resources(
            _ROOT_DIR / 'helium-chromium' / 'resources' / 'generate_resources.txt',
            _ROOT_DIR / 'helium-chromium' / 'resources'
        )

        replace_resources.copy_resources(
            _ROOT_DIR / 'helium-chromium' / 'resources' / 'helium_resources.txt',
            _ROOT_DIR / 'helium-chromium' / 'resources',
            source_tree
        )

        _configure_remoteexec(source_tree)

        if not args.ci:
            downloads_cache.mkdir(parents=True, exist_ok=True)
            _save_applied_patches(downloads_cache, applied_entries)
            prepare_fingerprint_path.write_text(
                f'{current_fingerprint}\ndev={args.dev}\ninputs={inputs_fingerprint}\n',
                encoding=ENCODING)
        progress.done()
    elif not args.ci:
        get_logger().info(
            'Source tree already prepared and unchanged since (patches, '
            'chromium_version.txt, etc. all match) -- skipping clone/'
            'download/patch/prune/substitution.')

    clang_format = shutil.which('clang-format')
    if not clang_format:
        parser.error('clang-format not found on PATH; run python -m pip install clang-format')
    formatter = source_tree / 'buildtools/win-format/clang-format.exe'
    formatter.parent.mkdir(parents=True, exist_ok=True)
    formatter.unlink(missing_ok=True)
    formatter.symlink_to(clang_format)

    args_gn_changed = False
    if not args.ci or not (source_tree / 'out/Default').exists():
        # Output args.gn
        (source_tree / 'out/Default').mkdir(parents=True, exist_ok=True)
        gn_flags = (_ROOT_DIR / 'helium-chromium' / 'flags.gn').read_text(encoding=ENCODING)
        gn_flags += '\n'
        windows_flags = (_ROOT_DIR / 'flags.windows.gn').read_text(encoding=ENCODING)
        if args.arm:
            windows_flags = windows_flags.replace('x64', 'arm64')
        if args.tarball or args.dev:
            windows_flags += '\nchrome_pgo_phase=0\n'

        if os.environ.get('SISO_REAPI_ADDRESS'):
            windows_flags += 'use_remoteexec = true\n'
            # Precompiled modules are not portable across Windows/Linux hosts.
            windows_flags += 'use_clang_modules = false\n'
        elif shutil.which('sccache'):
            windows_flags += 'cc_wrapper = "sccache"\n'

        gn_flags += windows_flags
        if args.dev:
            gn_flags += 'is_component_build=true\n'
            # chrome://settings and friends otherwise default optimize_webui
            # to !is_debug (see ui/webui/webui_features.gni) -- since we keep
            # is_debug=false even in --dev (a full debug build is far slower
            # to compile/run), force it off explicitly so WebUI TS/HTML
            # changes iterate as fast as the C++ side's component build does.
            gn_flags += 'optimize_webui=false\n'
            # build/config/pch.gni only enables precompiled headers when
            # !is_official_build -- a --dev build is the first config on this
            # toolchain to ever hit that path, and it triggers a clang error
            # ("missing 'export module' declaration in module interface
            # unit") compiling base/precompile.cc under our pinned
            # bleeding-edge clang + /std:c++23preview. Disable PCH explicitly
            # rather than chase that upstream clang issue; --dev's real speed
            # win is the component build + unbundled WebUI above, not PCH.
            gn_flags += 'enable_precompiled_headers=false\n'
        else:
            gn_flags += 'is_official_build=true\n'

        winsparkle_ed_key = os.environ.get('WINSPARKLE_ED_KEY', '')
        authenticode_org = os.environ.get('WINSPARKLE_AUTHENTICODE_ORG', '')
        # The Helium updater (WinSparkle + helper) is never wanted in a portable build,
        # even if the repo variables happen to be set.
        if winsparkle_ed_key and authenticode_org and not args.portable_only:
            gn_flags += 'enable_winsparkle=true\n'
            gn_flags += f'winsparkle_ed_key="{winsparkle_ed_key}"\n'
            gn_flags += f'winsparkle_authenticode_org="{authenticode_org}"\n'

        # Only touch args.gn when its content changes: a new mtime alone makes
        # Siso reload build.ninja, and `gn gen` below only has to run if it differs.
        args_gn = source_tree / 'out/Default/args.gn'
        previous_args = (args_gn.read_text(encoding=ENCODING).replace('\r\n', '\n')
                         if args_gn.exists() else None)
        if previous_args != gn_flags.replace('\r\n', '\n'):
            args_gn.write_text(gn_flags, encoding=ENCODING)
            args_gn_changed = True

    # Enter source tree to run build commands
    os.chdir(source_tree)

    # build.ninja regenerates itself when a BUILD.gn or args.gn changes, so a local
    # run only needs an explicit `gn gen` after this run changed the tree or the
    # GN args; CI only when there is no build.ninja yet.
    if (not os.path.exists('out\\Default\\build.ninja')
            or (not args.ci and (tree_changed or args_gn_changed))):
        # Run gn gen
        _run_build_process(
            'buildtools\\win\\gn.exe', 'gen', 'out\\Default', '--fail-on-unused-args')

    # Ninja commandline
    os.environ['SISO_PATH'] = str(source_tree / 'third_party/siso/cipd/siso.exe')
    ninja_commandline = [sys.executable, 'third_party\\depot_tools\\autoninja.py']
    if args.thread_count is not None:
        ninja_commandline.append('-j')
        ninja_commandline.append(args.thread_count)
    ninja_commandline.append('-C')
    ninja_commandline.append('out\\Default')

    # Finish all release targets before signing. Siso can replace signed outputs
    # if it is invoked again to build their dependents (the mini installer).
    ninja_commandline.append('chrome')
    if not args.portable_only:
        # chromedriver is not part of the portable ZIP (FILES.cfg lists it for 32bit only).
        ninja_commandline.extend(['chromedriver', 'setup', 'mini_installer'])

    # Run ninja
    if args.ci:
        # Leaves ~45 minutes of the 6 hour job limit for zipping and uploading the build
        # tree (the zip alone took ~14 minutes on a full tree).
        max_time = 5.25 * 60 * 60
        secs_spent = int(time.time()) - args.ci
        timeout = int(max_time - secs_spent)
        print(f"{timeout} seconds left for build")

        _run_build_process_timeout(*ninja_commandline, timeout=timeout)
    else:
        _run_build_process(*ninja_commandline)


if __name__ == '__main__':
    main()
