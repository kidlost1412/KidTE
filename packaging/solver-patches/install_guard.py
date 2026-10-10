"""Apply the exact audited solver patch on a chosen build host, never guess."""
import argparse
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def apply_guard(solver_root):
    manifest = json.loads((HERE / 'manifest.json').read_text(encoding='utf-8'))
    package = Path(solver_root) / 'src' / 'tiktok_live_studio_captcha_solver'
    target = package / 'solver.py'
    old = target.read_bytes()
    old_hash = hashlib.sha256(old).hexdigest()
    accepted = {manifest['solver_before_sha256'], manifest['solver_after_sha256'],
                *manifest.get('solver_prior_after_sha256', [])}
    if old_hash not in accepted:
        raise ValueError('solver_source_not_audited')
    patched = (HERE / 'solver-after-legacy-guard.py.snapshot').read_bytes()
    helper = (HERE / 'legacy_guard.py').read_bytes()
    if hashlib.sha256(patched).hexdigest() != manifest['solver_after_sha256']:
        raise ValueError('solver_patch_checksum_invalid')
    # Git may normalize a Python helper's newlines on Windows; semantic source
    # is pinned to LF for reproducible package hashes.
    helper = helper.replace(b'\r\n', b'\n')
    if hashlib.sha256(helper).hexdigest() != manifest['legacy_guard_sha256']:
        raise ValueError('solver_guard_checksum_invalid')
    episode = (HERE / 'episode_guard.py').read_bytes().replace(b'\r\n', b'\n')
    if hashlib.sha256(episode).hexdigest() != manifest['episode_guard_sha256']:
        raise ValueError('solver_episode_guard_checksum_invalid')
    if old_hash == manifest['solver_before_sha256']:
        backup = package / 'solver.before-legacy-guard.snapshot'
        if not backup.exists(): backup.write_bytes(old)
    for path, content in ((package / 'legacy_guard.py', helper),
            (package / 'episode_guard.py', episode), (target, patched)):
        temporary = path.with_suffix(path.suffix + '.guard-staging')
        temporary.write_bytes(content)
        os.replace(temporary, path)
    return {'ok': True, 'solver_sha256': manifest['solver_after_sha256']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--solver-root', required=True)
    print(json.dumps(apply_guard(parser.parse_args().solver_root)))
