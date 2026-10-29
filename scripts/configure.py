#!/usr/bin/env python3
"""Render local paths into ignored runtime tools; never alter the source snapshot."""
import argparse,json,os,shutil,sys
from pathlib import Path
from common import ROOT,configuration,render

def warn(message):
    """Diagnostics go to stderr: launcher/run.sh keep stdout for their own output."""
    print('configure:',message,file=sys.stderr)

def rehome_prefix(c,requested):
    """The default prefix is derived from the host home, so a checkout that moved to another
    machine or account must follow the new home instead of keeping the previous one (issue #142)."""
    home=Path.home()
    if requested is not None:return str(requested)
    saved=c.get('prefix')
    if not saved:return str(home/'.darling')
    old_home=c.get('host_home')
    if old_home and old_home!=str(home):
        if saved==str(Path(old_home)/'.darling'):
            warn(f'previous home {old_home} is gone; the default prefix moved to {home/".darling"}')
            return str(home/'.darling')
        try:relative=Path(saved).is_relative_to(old_home)
        except (TypeError,ValueError):relative=False
        if relative:
            warn(f'prefix {saved} is inside the previous home {old_home}; pass --prefix to move it')
    return saved

def link_build(runtime):
    """build/runtime/build must resolve to this checkout's build directory. A relative link keeps
    working after the checkout moves, and any absolute or dangling leftover is replaced instead of
    being treated as already correct (issue #142)."""
    link=runtime/'build'
    target='..'  # build/runtime/build -> build
    if link.is_symlink():
        if os.readlink(link)==target:return
        link.unlink()
    elif os.path.lexists(link):
        warn(f'{link} is not a symlink; leaving it untouched')
        return
    link.symlink_to(target,target_is_directory=True)

def stale_cache(build):
    """Return (cache, recorded source directory) when the CMake cache belongs to another checkout."""
    cache=build/'darling-src/build/CMakeCache.txt'
    try:lines=cache.read_text(errors='replace').splitlines()
    except OSError:return None
    expected=os.path.realpath(build/'darling-src')
    for line in lines:
        key,_,value=line.partition('=')
        if key.split(':')[0]!='CMAKE_HOME_DIRECTORY':continue
        if os.path.realpath(value)!=expected:return cache,value
        return None
    return None

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--guest-user');p.add_argument('--prefix',type=Path);p.add_argument('--sdk',type=Path)
a=p.parse_args();c=configuration()
if a.guest_user:
    if not a.guest_user.replace('_','').replace('-','').isalnum():p.error('guest user must be a simple account name')
    c['guest_user']=a.guest_user;c['guest_home']='/Users/'+a.guest_user
# Resolve the prefix before overwriting host_home, so it can still see the previous home.
c['prefix']=rehome_prefix(c,a.prefix.expanduser().resolve() if a.prefix else None)
if a.sdk:c['sdk']=str(a.sdk.expanduser().resolve())
c['project_root']=str(ROOT);c['host_home']=str(Path.home())
build=ROOT/'build';build.mkdir(exist_ok=True)
(build/'config.json').write_text(json.dumps(c,indent=2)+'\n')
folder=build/'runtime/tools';folder.mkdir(parents=True,exist_ok=True)
for source in (ROOT/'tools').iterdir():
    if not source.is_file():continue
    target=folder/source.name
    # Unlink the directory entry, including dangling symlinks. Any other error
    # must abort rather than following a leftover link or writing through it.
    target.unlink(missing_ok=True)
    target.write_text(render(source.read_text(),c))
    os.chmod(target,source.stat().st_mode & 0o777)
link_build(folder.parent)
stale=stale_cache(build)
if stale:
    cache,previous=stale
    warn(f'{cache} was generated for {previous}, not {build/"darling-src"}; builds will fail with '
         f'missing files or permission errors against the old path. Remove {build/"darling-src/build"} '
         'and build again (it is never removed automatically because it may hold local work).')
print('Configured',len(list(folder.iterdir())),'runtime sources for',c['guest_home'])
