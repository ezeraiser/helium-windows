#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2025 The Helium Authors
# You can use, redistribute, and/or modify this source code under
# the terms of the GPL-3.0 license that can be found in the LICENSE file.
"""
Checks that patches/ezer/*.patch still apply cleanly on top of the current
upstream Chromium + Helium patch stack.

Run this after every imputnet sync (fetching and merging/fast-forwarding
helium-chromium), before starting a full build. Upstream and Helium patches
change the same files our ezer patches touch, so a sync can silently break
their context; this catches that in minutes instead of after a full
clone + build cycle.

Requires build/src to already exist as a git checkout (from a previous
build.py run) -- this resets it in place with `git fetch`/`reset`/`clean`
rather than doing a fresh clone, since only the file text is needed, not a
compilable tree. Leaves build/src with only the non-ezer patches applied;
running build.py afterwards resets and reapplies everything properly, so
this has no lasting side effects.
"""

import sys
import os
import stat
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'helium-chromium' / 'utils'))
import patches
import prune_binaries
from _common import get_logger, get_chromium_version, parse_series
sys.path.pop(0)

_ROOT_DIR = Path(__file__).resolve().parent


def _clear_readonly_tree(path):
    """See build.py's copy of this helper: CIPD/npm mark some files
    read-only on Windows, which makes `git clean` fail instead of removing
    them. Best-effort; a file we truly have no access to is left for the
    normal error to surface."""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            entry = Path(root) / name
            try:
                os.chmod(entry, stat.S_IWRITE)
            except OSError as exc:
                get_logger().warning('Could not clear read-only attribute on %s: %s', entry, exc)


def main():
    source_tree = _ROOT_DIR / 'build' / 'src'
    if not (source_tree / '.git').exists():
        get_logger().error(
            'build/src is not a git checkout yet. Run build.py at least once first '
            'so there is a tree to check the ezer patches against.')
        sys.exit(1)

    patches_dir = _ROOT_DIR / 'helium-chromium' / 'patches'
    series = list(parse_series(patches_dir / 'series'))
    ezer_patches = [p for p in series if str(p).replace('\\', '/').startswith('ezer/')]
    other_patches = [p for p in series if p not in ezer_patches]

    if not ezer_patches:
        get_logger().info("No ezer/ patches in patches/series, nothing to check.")
        return

    chromium_version = get_chromium_version()
    get_logger().info('Resetting build/src to a clean %s checkout...', chromium_version)
    _clear_readonly_tree(source_tree)
    # With Git for Windows' default core.autocrlf=true, the reset below would write every file
    # that differs between two Chromium versions with CRLF line endings, and the patches
    # (LF) then fail with "different line endings".
    subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=source_tree, check=True)
    subprocess.run(['git', 'fetch', 'origin', 'tag', chromium_version, '--depth=2'],
                   cwd=source_tree, check=True)
    subprocess.run(['git', 'reset', '--hard', 'FETCH_HEAD'], cwd=source_tree, check=True)
    subprocess.run(['git', 'clean', '-ffdx', '-e', 'uc_staging'], cwd=source_tree, check=True)

    # The bundled patch.exe (third_party/git) comes from a downloaded package
    # this script doesn't unpack, since that needs a full build.py run. Let
    # patches.find_and_check_patch() fall back to PATCH_BIN or "patch" on
    # PATH (e.g. Git for Windows' usr/bin/patch, already used elsewhere in
    # this session) instead.
    patch_bin_path = None

    # Some baseline patches (e.g. ungoogled-chromium/fix-building-with-prunned-binaries.patch)
    # only apply to a pruned tree, matching build.py's order: download -> prune -> patch.
    # Files that pruning.list names but this tree lacks (DEPS-downloaded content such as
    # chromium-bidi's node_modules) are legitimately absent here, same as in build.py.
    get_logger().info('Pruning binaries (pruning.list)...')
    prune_binaries.prune_files(
        source_tree,
        (_ROOT_DIR / 'helium-chromium' / 'pruning.list').read_text(
            encoding='UTF-8').splitlines())

    # Apply one at a time: a few baseline patches (devtools-frontend, puffin,
    # search-engine-data) depend on DEPS-downloaded content that this shortcut never
    # unpacks, so they are expected to fail. Don't let that stop the ezer check, but
    # list them, since an ezer patch that touches the same files may then fail too.
    get_logger().info('Applying %d non-ezer patch(es) as a baseline...', len(other_patches))
    baseline_failures = []
    for other_patch in other_patches:
        try:
            patches.apply_patches([patches_dir / other_patch], source_tree,
                                  patch_bin_path=patch_bin_path)
        except subprocess.CalledProcessError:
            baseline_failures.append(other_patch)
    if baseline_failures:
        get_logger().warning(
            '%d baseline patch(es) did not apply (expected for patches needing DEPS '
            'content): %s', len(baseline_failures),
            ', '.join(str(f) for f in baseline_failures))

    get_logger().info("Checking %d ezer/ patch(es)...", len(ezer_patches))
    failures = []
    for ezer_patch in ezer_patches:
        patch_path = patches_dir / ezer_patch
        code, out, err = patches.dry_run_check(patch_path, source_tree, patch_bin_path=patch_bin_path)
        if code == 0:
            get_logger().info('  OK    %s', ezer_patch)
            # ezer patches stack (several touch settings_strings.grdp, appearance_page.ts,
            # prefs_util.cc...): apply each one that checks out so the next is checked
            # against the tree it will really see, not against the bare baseline.
            patches.apply_patches([patch_path], source_tree, patch_bin_path=patch_bin_path)
        else:
            failures.append(ezer_patch)
            get_logger().error('  FAIL  %s', ezer_patch)
            for line in (out + err).splitlines():
                get_logger().error('        %s', line)

    if failures:
        get_logger().error('%d ezer patch(es) no longer apply cleanly: %s', len(failures),
                           ', '.join(str(f) for f in failures))
        get_logger().error('Fix their context against build/src before running a full build.')
        sys.exit(1)

    get_logger().info('All ezer/ patches apply cleanly against %s.', chromium_version)


if __name__ == '__main__':
    main()
