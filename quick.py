#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2026 The Helium Authors
# You can use, redistribute, and/or modify this source code under
# the terms of the GPL-3.0 license that can be found in the LICENSE file.
"""
Fast loop for changes to `helium-chromium/patches/ezer/*.patch`.

`python build.py` re-prepares the whole tree whenever a patch changed (about an
hour and a half) and then recompiles most of it. This script instead brings the
already prepared `build/src` up to date *in place*, the way `build.py --dev`
does, but also for a normal (non --dev) tree, and then only compiles what
changed:

    python quick.py sync            undo/redo the changed patches in build/src
    python quick.py check           sync, then compile just the changed .cc files
    python quick.py build           sync, then `autoninja chrome`
    python quick.py build --package ... and package the portable x64 ZIP
    python quick.py adopt           declare that build/src already contains the
                                    current patches (see below)

The patches that are applied are recorded in build/download_cache/
applied_patches (the same record `build.py` keeps). `sync` reverses the saved
copies from the first changed patch onwards (strictly, no fuzz) and applies the
current ones. It is all-or-nothing: the files those patches touch are backed up
first and restored if anything does not undo or apply cleanly.

This does NOT update `prepare_fingerprint.txt`: a later plain `python build.py`
still sees changed patches and prepares from scratch, which stays the reference
for anything you release (domain/name substitution, i18n and PGO only happen
there).

`adopt` is for the one case this cannot handle: you edited build/src by hand
to match the current patches (so the saved copies no longer describe the tree).
It records the current patches as applied, without touching the tree.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'build' / 'src'
CACHE = ROOT / 'build' / 'download_cache'
BACKUP = ROOT / 'build' / 'quick_backup'
STATE = CACHE / 'quick_state.json'
MAX_TAIL = 25
DEFAULT_JOBS = 8


def load_build_module():
    spec = importlib.util.spec_from_file_location('helium_build_py', ROOT / 'build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hb = load_build_module()
sys.path.insert(0, str(ROOT / 'helium-chromium' / 'utils'))
import patches  # noqa: E402
import i18n_apply  # noqa: E402

sys.path.pop(0)


def key(entry):
    return (entry['label'], entry['rel'], entry['sha256'])


def touched_files(patch_text):
    """Paths (relative to the tree) a patch creates, changes or deletes."""
    paths = []
    for line in patch_text.splitlines():
        if line.startswith('+++ b/'):
            paths.append(line[len('+++ b/'):].split('\t')[0].strip())
    return paths


def die(message):
    print(f'quick.py: {message}', file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- sync -------

def saved_path(entry):
    return CACHE / hb._APPLIED_PATCHES_DIR / entry['file']


def read_text(path):
    return path.read_text(encoding='utf-8', errors='replace')


def plan(old, new):
    """What has to be undone and (re)applied.

    The changed patches, plus every later, unchanged patch that touches a file
    one of them touches (directly or through another such patch): those sit on
    top of the changed ones, so their hunks only fit once the changed patch is
    out of the way. Patches on other files commute with all of this and are
    left applied. Returns (entries to undo as recorded, in old order; entries
    to apply, in new order)."""
    old_by_key = {key(e): e for e in old}
    new_keys = {key(e) for e in new}
    undo = [e for e in old if key(e) not in new_keys]
    apply = [e for e in new if key(e) not in old_by_key]
    if not undo and not apply:
        return [], []

    first = min([i for i, e in enumerate(old) if key(e) not in new_keys] +
                [i for i, e in enumerate(new) if key(e) not in old_by_key])
    files = set()
    for entry in undo:
        files.update(touched_files(read_text(saved_path(entry))))
    for entry in apply:
        files.update(touched_files(read_text(entry['path'])))

    redo = {}
    grew = True
    while grew:
        grew = False
        for index, entry in enumerate(new):
            if index <= first or key(entry) not in old_by_key or key(entry) in redo:
                continue
            mine = set(touched_files(read_text(entry['path'])))
            if mine & files:
                redo[key(entry)] = entry
                files |= mine
                grew = True

    undo = sorted(undo + [old_by_key[k] for k in redo], key=old.index)
    apply = sorted(apply + list(redo.values()), key=new.index)
    return undo, apply


def backup_files(paths):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = BACKUP / stamp
    target.mkdir(parents=True)
    absent = []
    for rel in sorted(set(paths)):
        src = SOURCE / rel
        if src.is_file():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target / rel)
        else:
            absent.append(rel)
    (target / '_absent.json').write_text(json.dumps(absent), encoding='utf-8')
    return target


def restore_backup(target):
    absent = json.loads((target / '_absent.json').read_text(encoding='utf-8'))
    for rel in absent:
        (SOURCE / rel).unlink(missing_ok=True)
    for path in target.rglob('*'):
        if path.is_file() and path.name != '_absent.json':
            rel = path.relative_to(target)
            (SOURCE / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, SOURCE / rel)


def sync():
    """Brings build/src up to date with the patches. Returns the .cc files (as
    tree-relative paths) touched by the patches that changed, or [] when
    nothing changed."""
    if not (SOURCE / 'BUILD.gn').exists():
        die('build/src is not prepared; run `python build.py` once first.')
    old = hb._load_applied_patches(CACHE)
    if old is None:
        die('there is no usable record of the applied patches '
            '(build/download_cache/applied_patches). If build/src already contains the '
            'current patches, run `python quick.py adopt`; otherwise run `python build.py`.')
    new = hb._patch_entries(ROOT)

    old_tail, new_tail = plan(old, new)
    if not old_tail and not new_tail:
        print('Patches: nothing changed since the last sync.')
        return []
    if max(len(old_tail), len(new_tail)) > MAX_TAIL:
        die(f'{len(old_tail)}/{len(new_tail)} patches affected (an upstream sync?); '
            'run a full `python build.py` instead.')

    saved = [saved_path(e) for e in old_tail]
    old_keys = {key(e) for e in old}
    changed_new = {key(e) for e in new} - old_keys

    texts = [read_text(p) for p in saved]
    texts += [read_text(e['path']) for e in new_tail]
    paths = [p for t in texts for p in touched_files(t)]

    print(f'Patches: undoing {len(old_tail)} and applying {len(new_tail)}: '
          + ', '.join(e['rel'] for e in new_tail or old_tail)
          + f' ({len(set(paths))} files involved).')
    backup = backup_files(paths)
    patch_bin = SOURCE / hb._PATCH_BIN_RELPATH
    try:
        if saved:
            patches.apply_patches(saved, SOURCE, reverse=True, patch_bin_path=patch_bin,
                                  fuzz=False)
        if new_tail:
            patches.apply_patches([e['path'] for e in new_tail], SOURCE,
                                  patch_bin_path=patch_bin)
    except Exception as exc:  # pylint: disable=broad-except
        print(f'quick.py: a patch did not undo/apply cleanly ({exc}); restoring the files...',
              file=sys.stderr)
        restore_backup(backup)
        die('restored build/src to its previous state. Fix the patch (or run a full '
            '`python build.py`).')

    hb._save_applied_patches(CACHE, new)
    shutil.rmtree(backup, ignore_errors=True)
    if BACKUP.exists() and not any(BACKUP.iterdir()):
        BACKUP.rmdir()

    changed = set()
    for entry in new:
        if key(entry) in changed_new:
            changed.update(touched_files(entry['path'].read_text(encoding='utf-8',
                                                                    errors='replace')))
    # Files only the old versions touched were reverted: they changed too.
    for text in texts[:len(saved)]:
        changed.update(touched_files(text))
    return sorted(p for p in changed if p.endswith('.cc') and (SOURCE / p).exists())


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state), encoding='utf-8')


def sync_translations():
    """Applies the translations of the languages whose file changed since the
    last run (Turkish on the very first run) into the XTB files."""
    translations = ROOT / 'helium-chromium' / 'i18n' / 'translations'
    state = load_state()
    hashes = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in translations.glob('*.json')}
    source_hash = hashlib.sha256(i18n_apply.SOURCE_PATH.read_bytes()).hexdigest()
    old_hashes = state.get('translations')
    if old_hashes is None:
        todo = ['tr'] if 'tr' in hashes else []
    else:
        todo = [c for c, h in hashes.items() if old_hashes.get(c) != h]
        if state.get('source') != source_hash and 'tr' in hashes and 'tr' not in todo:
            todo.append('tr')
    if todo:
        source = json.loads(i18n_apply.SOURCE_PATH.read_text(encoding='utf-8'))
        i18n_apply.namesub.add_grit_to_path(SOURCE)
        index = i18n_apply.build_xtb_index(source, SOURCE)
        for code in todo:
            i18n_apply.apply_language((code, source, index))
    state.update({'translations': hashes, 'source': source_hash})
    save_state(state)


# ---------------------------------------------------------------- compile ----

def ninja_env():
    env = dict(os.environ)
    env['SISO_PATH'] = str(SOURCE / 'third_party/siso/cipd/siso.exe')
    return env


def run_ninja(targets, jobs):
    """Like build.py: inside a shell with the Visual Studio variables, because
    a changed BUILD.gn makes ninja regenerate its files with `gn`, and `gn`
    only finds the toolchain there."""
    os.environ.update(ninja_env())
    command = [sys.executable, 'third_party\\depot_tools\\autoninja.py', '-j', str(jobs), '-C',
               'out\\Default', *targets]
    try:
        hb._run_build_process(*command, cwd=SOURCE)
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


def object_targets(sources):
    """Maps each source file to its object target, by looking for the compile
    rule in the .ninja files of its directory and the directories above."""
    out = SOURCE / 'out' / 'Default'
    targets, missing = [], []
    for rel in sources:
        pattern = re.compile(r'^build (obj/\S+?\.obj): cxx \.\./\.\./' + re.escape(rel) + r'\b',
                             re.MULTILINE)
        found = None
        directory = Path(rel).parent
        while found is None and str(directory) not in ('.', ''):
            for ninja in sorted((out / 'obj' / directory).glob('*.ninja')):
                match = pattern.search(ninja.read_text(encoding='utf-8', errors='replace'))
                if match:
                    found = match.group(1)
                    break
            directory = directory.parent
        (targets if found else missing).append(found or rel)
    return targets, missing


def newest_zip():
    zips = sorted((ROOT / 'build').glob('signing-*/artifacts/*.zip'),
                  key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('sync', 'check', 'build', 'adopt'))
    parser.add_argument('--package', action='store_true',
                        help='build: also package the portable x64 ZIP (unsigned)')
    parser.add_argument('--all', action='store_true',
                        help='check: compile every .cc file any ezer patch touches')
    parser.add_argument('--yes', action='store_true', help='adopt: do not ask')
    parser.add_argument('-j', '--jobs', type=int, default=DEFAULT_JOBS,
                        help=f'parallel compile jobs (default {DEFAULT_JOBS})')
    args = parser.parse_args()

    if args.command == 'adopt':
        if not args.yes:
            print('This records the CURRENT patches as already applied to build/src, without '
                  'touching the tree. Only do it if build/src really contains them.')
            if input('Continue? [y/N] ').strip().lower() != 'y':
                return 1
        hb._save_applied_patches(CACHE, hb._patch_entries(ROOT))
        sync_translations()
        print('Recorded the current patches as applied.')
        return 0

    changed_cc = sync()
    sync_translations()
    # .cc files whose patch changed but that were not compiled since: a `sync`
    # (or an earlier, failed `check`) must not make `check` forget them.
    state = load_state()
    pending = sorted(set(state.get('pending_cc', [])) | set(changed_cc))
    state['pending_cc'] = pending
    save_state(state)
    if args.command == 'sync':
        print('.cc files to compile on the next check/build:', *(pending or ['(none)']),
              sep='\n  ')
        return 0

    if args.command == 'check':
        sources = [p for p in pending if (SOURCE / p).exists()]
        if args.all:
            sources = sorted({p for e in hb._patch_entries(ROOT) if '/ezer/' in str(e['path']).replace('\\', '/')
                              for p in touched_files(e['path'].read_text(encoding='utf-8'))
                              if p.endswith('.cc') and (SOURCE / p).exists()})
        if not sources:
            print('No .cc file changed; nothing to compile (use --all, or `build`).')
            return 0
        targets, missing = object_targets(sources)
        for rel in missing:
            print(f'  (no compile rule found for {rel}; it is checked by `build`)')
        if not targets:
            return 0
        print('Compiling:', *targets, sep='\n  ')
        code = run_ninja(targets, args.jobs)
        print('quick check: OK' if code == 0 else 'quick check: FAILED')
        if code == 0:
            state['pending_cc'] = [p for p in pending if p not in sources]
            save_state(state)
        return code

    code = run_ninja(['chrome'], args.jobs)
    if code != 0:
        print('quick build: FAILED')
        return code
    print('quick build: OK')
    state['pending_cc'] = []
    save_state(state)
    if args.package:
        code = subprocess.run([sys.executable, 'signing.py', '--arch', 'x64', '--no-sign'],
                              cwd=ROOT, check=False).returncode
        if code != 0:
            return code
        print('ZIP:', newest_zip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
