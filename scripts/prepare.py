#!/usr/bin/env python3
"""Fetch pinned Darling sources and apply each patch to its owning repository."""
import argparse,json,os,subprocess,tempfile,sys
from pathlib import Path
# --check-only must not create import caches in the source package either.
sys.dont_write_bytecode=True
from common import ROOT,render
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--source',type=Path,default=ROOT/'build/darling-src')
p.add_argument('--jobs',type=int,default=4,help='parallel submodule fetches')
p.add_argument('--check-only',action='store_true',help='validate against temporary base indexes, without changing existing source')
a=p.parse_args();source=a.source.resolve();manifest=json.loads((ROOT/'upstream/manifest.json').read_text())
def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)
# The pinned darling-swift .lfsconfig points at the retired git-lfs.darlinghq.org; GitHub still serves the objects.
LFS_URL='lfs.url=https://github.com/darlinghq/darling-swift.git/info/lfs'
# Without these, a checkout writes Git LFS pointer text where a real dylib belongs, which links but crashes.
LFS_FILTERS=['-c','filter.lfs.required=true','-c','filter.lfs.smudge=git-lfs smudge -- %f',
             '-c','filter.lfs.process=git-lfs filter-process','-c','filter.lfs.clean=git-lfs clean -- %f']
def submodules(checkout):
    out=subprocess.run(['git','-C',str(checkout),'submodule','status','--recursive'],text=True,capture_output=True).stdout
    return [checkout/line.split()[1] for line in out.splitlines() if len(line.split())>1]
def pointers(repo):
    """Tracked files still checked out as LFS pointer text instead of their real content."""
    out=subprocess.run(['git','-C',str(repo),'lfs','ls-files'],text=True,capture_output=True).stdout
    return [f for f in (line.split(maxsplit=2) for line in out.splitlines()) if len(f)==3 and f[1]=='-']
def repair(checkout):
    """An interrupted fetch leaves a submodule with a wiped index or pointer text and 'submodule update'
    treats both as up to date, so check every worktree before this checkout becomes the source tree."""
    for repo in [checkout,*submodules(checkout)]:
        name=repo.relative_to(checkout) or '.'
        if repo!=checkout and subprocess.run(['git','-C',str(repo),'status','--porcelain'],text=True,
                                             capture_output=True).stdout.strip():
            print(f'Repairing the interrupted checkout of {name}',flush=True)
            run(['git','-C',str(repo),'checkout','-f','HEAD'],stdout=subprocess.DEVNULL)
        if pointers(repo):
            print(f'Fetching the Git LFS objects of {name} (pointer text is not a library)',flush=True)
            run(['git','-C',str(repo),'lfs','install','--local'],stdout=subprocess.DEVNULL)
            run(['git','-C',str(repo),*(['-c',LFS_URL] if 'swift' in name.parts else []),'lfs','pull'],
                stdout=subprocess.DEVNULL)
            if pointers(repo):raise SystemExit(f'{name}: Git LFS objects are still missing; install git-lfs and run this again')

def applies(repo,patch,reverse=False):
    return subprocess.run(['git','-C',str(repo),'apply',*(['--reverse'] if reverse else []),'--check','-'],input=patch,text=True,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
# The rendered text of each patch as last applied, so a later version can revert exactly it.
applied=ROOT/'build/applied-patches'
def record_patch(component,patch):
    applied.mkdir(parents=True,exist_ok=True);(applied/Path(component['patch']).name).write_text(patch)
def previous_patches(component):
    """Earlier applied versions of a component's patch: the recorded copy, then each version in this repository's history."""
    recorded=applied/Path(component['patch']).name
    if recorded.exists():yield recorded.read_text()
    history=subprocess.run(['git','-C',str(ROOT),'log','--format=%H','--',component['patch']],text=True,capture_output=True).stdout.split()
    for commit in history:
        shown=subprocess.run(['git','-C',str(ROOT),'show',f"{commit}:{component['patch']}"],text=True,capture_output=True)
        if shown.returncode==0:yield render(shown.stdout)
if not (source/'.git').exists():
    if a.check_only:p.error('check-only requires an existing pinned source checkout')
    if source.exists():p.error('refusing non-checkout destination; choose an empty source path')
    source.parent.mkdir(parents=True,exist_ok=True)
    # Fetch the exact pin, not today's master. Publish only a complete checkout;
    # failed downloads are removed without touching an existing user checkout.
    with tempfile.TemporaryDirectory(prefix='darling-clone-',dir=source.parent) as temporary:
        checkout=Path(temporary)/'source'
        run(['git','init',str(checkout)])
        run(['git','-C',str(checkout),'remote','add','origin',manifest['upstream']])
        run(['git','-C',str(checkout),'fetch','--depth','1','origin',manifest['revision']])
        run(['git','-C',str(checkout),'checkout','--detach','FETCH_HEAD'])
        if subprocess.run(['git','lfs','version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            p.error('Git LFS is required to fetch the pinned sources; install git-lfs first')
        run(['git','-C',str(checkout),*LFS_FILTERS,'lfs','install','--local'],stdout=subprocess.DEVNULL)
        print('Initializing Darling submodules (large repositories/LFS assets may take a while)...',flush=True)
        for attempt in range(1,4):
            try:
                run(['git','-C',str(checkout),'-c',LFS_URL,*LFS_FILTERS,'submodule','update','--init','--recursive',
                     '--depth','1','--jobs',str(a.jobs),'--progress'])
                break
            except subprocess.CalledProcessError:
                if attempt==3:raise
                print(f'Submodule fetch interrupted (attempt {attempt}/3). Retrying...',flush=True)
        repair(checkout)
        checkout.rename(source)

# Never reset someone else's checkout to make a patch apply.
for component in manifest['components']:
    repo=source/component['path']
    actual=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    if actual!=component['revision']:raise SystemExit(f"Wrong revision for {component['path']}: expected {component['revision']}, got {actual}")
    patch=render((ROOT/component['patch']).read_text())
    if a.check_only:
        with tempfile.TemporaryDirectory(prefix='darling-patch-index-') as tmp:
            env=dict(os.environ,GIT_INDEX_FILE=str(Path(tmp)/'index'))
            run(['git','-C',str(repo),'read-tree',component['revision']],env=env)
            run(['git','-C',str(repo),'apply','--cached','--check','--whitespace=nowarn','-'],input=patch,text=True,env=env)
    else:
        already=applies(repo,patch,reverse=True)
        if not already and not applies(repo,patch):
            # An older version of this patch is applied (the checkout predates a pull). Revert exactly that
            # version, never a reset, so other edits in the repository survive.
            old=next((text for text in previous_patches(component) if applies(repo,text,reverse=True)),None)
            if old is None:raise SystemExit(f"{component['path']}: {component['patch']} neither applies nor matches an earlier applied version; "
                                             f"the checkout has other edits. Commit or discard them in {repo}, then run this again.")
            print(f"Replacing the earlier {component['patch']} in {component['path']}",flush=True)
            run(['git','-C',str(repo),'apply','--reverse','--whitespace=nowarn','-'],input=old,text=True)
            if not applies(repo,patch):
                run(['git','-C',str(repo),'apply','--whitespace=nowarn','-'],input=old,text=True)
                raise SystemExit(f"{component['path']}: the new {component['patch']} does not apply to the pinned revision; the earlier version was restored")
        if not already:
            run(['git','-C',str(repo),'apply','--whitespace=nowarn','-'],input=patch,text=True)
        record_patch(component,patch)
    print(('Checked ' if a.check_only else 'Prepared ')+component['path'],flush=True)
print('All',len(manifest['components']),'component patches validated' if a.check_only else 'component patches ready')
