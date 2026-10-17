#!/usr/bin/env python3
"""Compile the patch's actual DNS-query guard against resolver stubs; no DNS traffic."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
patch = (root / 'patches/Libinfo.patch').read_text()
added = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
match = re.search(r'static int\ndr_query\([^}]*\{.*?\n\}', added, re.S)
if not match:
    raise SystemExit('dr_query implementation missing from patch')
source = r'''
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdio.h>
#define ns_t_a 1
#define ns_t_aaaa 28
struct __res_state {int unused;};
static int res_ninit(struct __res_state *s) {return 0;}
static int res_nquery(struct __res_state *s,const char *n,int c,int t,unsigned char *a,int l) {return 42;}
static void res_ndestroy(struct __res_state *s) {}
''' + match.group() + r'''
#define EXPECT(host,type,result) do { if(dr_query(host,1,type,0,0)!=(result)) {fprintf(stderr,"FAIL %s\n",host);return 1;} } while(0)
int main(void) {
 unsetenv("RCLOL_CDN_IPV4_ONLY");
 EXPECT("lol.dyn.riotcdn.net",ns_t_aaaa,42);
 setenv("RCLOL_CDN_IPV4_ONLY","1",1);
 EXPECT("lol.dyn.riotcdn.net",ns_t_aaaa,-1);
 EXPECT("LOL.SECURE.DYN.RIOTCDN.NET",ns_t_aaaa,-1);
 EXPECT("lol.dyn.riotcdn.net",ns_t_a,42);
 EXPECT("example.com",ns_t_aaaa,42);
 EXPECT("riotcdn.net.example.com",ns_t_aaaa,42);
 EXPECT("notriotcdn.net",ns_t_aaaa,42);
 setenv("RCLOL_CDN_IPV4_ONLY","0",1);
 EXPECT("lol.dyn.riotcdn.net",ns_t_aaaa,42);
 puts("CDN_DNS_OFFLINE pass=1 opt_out=1 other_domains_preserved=1");
 return 0;
}
'''
compiler = shutil.which('cc') or shutil.which('clang')
if not compiler:
    raise SystemExit('A native C compiler is required')
with tempfile.TemporaryDirectory(prefix='riot-cdn-dns-check-') as directory:
    folder = Path(directory)
    (folder / 'check.c').write_text(source)
    subprocess.run([compiler, str(folder / 'check.c'), '-o', str(folder / 'check')], check=True)
    subprocess.run([str(folder / 'check')], check=True)
