"""Parse every owned source file and record an exact inventory for release QA."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = ('backend/src','backend/tests','backend/tools','plugins','extension',
         'extension-shopee','packaging','ui')
EXTENSIONS = {'.py','.spec','.js','.mjs','.cjs','.json','.html','.css','.ps1','.cs','.iss'}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key:' + key)
        value[key] = item
    return value


def main():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node required for JavaScript syntax audit')
    inventory, errors = [], []
    for tree in TREES:
        for file in sorted((ROOT/tree).rglob('*')):
            if not file.is_file() or file.suffix not in EXTENSIONS or '__pycache__' in file.parts:
                continue
            raw = file.read_bytes()
            source = raw.decode('utf-8-sig')
            relative = file.relative_to(ROOT).as_posix()
            inventory.append({'path':relative,'lines':len(source.splitlines()),
                              'sha256':hashlib.sha256(raw).hexdigest()})
            try:
                if file.suffix in {'.py','.spec'}:
                    ast.parse(source,filename=relative)
                elif file.suffix == '.json':
                    json.loads(source,object_pairs_hook=unique_object)
                elif file.suffix in {'.js','.mjs','.cjs'}:
                    result = subprocess.run([node,'--check','--input-type=module'], input=source,
                        text=True,encoding='utf-8',capture_output=True,timeout=15)
                    if result.returncode:
                        raise ValueError(result.stderr)
                    for reference in re.findall(r'(?:from\s*|import\s*\(?)\s*[\'\"](\.[^\'\"]+)[\'\"]',source):
                        if not (file.parent/reference).is_file():
                            raise ValueError('missing_import:' + reference)
            except (SyntaxError,ValueError,subprocess.SubprocessError) as error:
                errors.append({'path':relative,'error':str(error)})
    for platform in ('extension','extension-shopee'):
        manifest = json.loads((ROOT/platform/'manifest.json').read_text(encoding='utf-8'))
        assets = [manifest['background']['service_worker'],manifest['action']['default_popup']]
        assets += list(manifest['icons'].values())
        assets += [name for row in manifest['content_scripts'] for name in row['js']]
        for name in assets:
            if not (ROOT/platform/name).is_file():
                errors.append({'path':platform+'/manifest.json','error':'missing_asset:'+name})
    report = {'files':len(inventory),'lines':sum(item['lines'] for item in inventory),
              'inventory':inventory,'errors':errors}
    output = ROOT/'backend/runtime/ui-proof/audit-inventory.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"AUDIT: {report['files']} source files, {report['lines']} lines, {len(errors)} errors")
    if errors:
        print(json.dumps(errors,ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
