#!/usr/bin/env python3
"""Validate the public source package without launching apps or installing anything."""
import ast,json,re,subprocess,sys
from pathlib import Path
from common import ROOT
subprocess.run([sys.executable,str(ROOT/'scripts/configure.py')],check=True)
try:
    files=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z'],stderr=subprocess.DEVNULL).decode().split('\0')
except (OSError, subprocess.CalledProcessError):
    files=[str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and not any(part.startswith('.') or part in ['build','__pycache__'] for part in p.relative_to(ROOT).parts)]
errors=[]
secret_patterns=[r'gh[pousr]_[A-Za-z0-9]{30,}',r'AKIA[0-9A-Z]{16}',r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
for name in filter(None,files):
    p=ROOT/name;data=p.read_bytes()
    # The Nix desktop package intentionally ships this icon, not a runtime binary.
    if name == 'darling-lol.png' and data.startswith(b'\x89PNG\r\n\x1a\n'):
        continue
    if data.startswith((b'\x7fELF',b'MZ',b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe')) or b'\0' in data:
        errors.append(name+': binary content is not part of this source package');continue
    try:text=data.decode()
    except UnicodeDecodeError:errors.append(name+': non-UTF8 content');continue
    if re.search(r'/(?:home|Users)/' + 'hi' + 'rew' + r'(?:/|\b)',text):
        errors.append(name+': development-machine path')
    if any(re.search(pattern,text) for pattern in secret_patterns):errors.append(name+': possible credential; review before publishing')
    if name.split('/')[0] in ['build','logs','Config','Library'] or p.suffix in ['.dylib','.so','.asar','.perf','.zip']:
        errors.append(name+': local artifact')
for p in list((ROOT/'scripts').glob('*.py'))+list((ROOT/'build/runtime/tools').glob('*.py')):
    try:ast.parse(p.read_text(),filename=str(p))
    except SyntaxError as e:errors.append(p.name+': '+str(e))
for p in list((ROOT/'scripts').glob('*.sh'))+list((ROOT/'build/runtime/tools').glob('*.sh')):
    result=subprocess.run(['bash','-n',str(p)],capture_output=True,text=True)
    if result.returncode:errors.append(p.name+': shell syntax error')
manifest=json.loads((ROOT/'upstream/manifest.json').read_text())
hunk_re=re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
for c in manifest['components']:
    if not re.fullmatch('[0-9a-f]{40}',c['revision']):errors.append(c['path']+': invalid pinned revision')
    if not (ROOT/c['patch']).is_file():errors.append(c['path']+': patch missing')
    else:
        patch_text=(ROOT/c['patch']).read_text()
        parsed=subprocess.run(['git','apply','--numstat','-'],input=patch_text,
                              text=True,capture_output=True)
        if parsed.returncode:errors.append(c['patch']+': '+parsed.stderr.strip())
        lines=patch_text.splitlines()
        i=0
        while i<len(lines):
            m=hunk_re.match(lines[i])
            if not m:i+=1;continue
            old_exp=int(m.group(2)) if m.group(2) is not None else 1
            new_exp=int(m.group(4)) if m.group(4) is not None else 1
            hunk_line=i+1;i+=1;old_got=new_got=0
            while i<len(lines) and not lines[i].startswith(('diff --git ','@@ ','--- ','+++ ')):
                ln=lines[i]
                if ln.startswith('-'):old_got+=1
                elif ln.startswith('+'):new_got+=1
                elif ln.startswith(' '):old_got+=1;new_got+=1
                elif ln=='' and i+1<len(lines) and not lines[i+1].startswith('diff --git '):old_got+=1;new_got+=1
                i+=1
            if old_got!=old_exp or new_got!=new_exp:
                errors.append(f"{c['patch']}:{hunk_line}: hunk count mismatch (header -{old_exp},+{new_exp}, actual -{old_got},+{new_got})")
if errors:
    print('\n'.join(errors));raise SystemExit(1)
print('PASS: tracked source audit, rendered Python/shell syntax and',len(manifest['components']),'pinned patch entries')
