"""Exercise the shipped host-backed timing provider without starting Darling."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class DisplayTimingProviderTests(unittest.TestCase):
    def test_publication_cache_generation_and_clock_mapping(self):
        source = runtime.CompatibilityChecks().patched(
            'cocotron', 'CoreGraphics/DarlingDisplayTiming.c')
        header = runtime.CompatibilityChecks().patched(
            'cocotron', 'CoreGraphics/include/CoreGraphics/DarlingDisplayTiming.h')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'DarlingDisplayTiming.h').write_text(header)
            (root / 'provider.c').write_text(source)
            (root / 'probe.c').write_text(r'''
#include "DarlingDisplayTiming.h"
#include <assert.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
static void checkOverrides(DGTimingSnapshot (*readTiming)(uint64_t,uint32_t,DGTimingCache*),
                           DGTimingCache *cache) {
    const char *values[]={"1","60","144","1000","  +144","59.94","144Hz","0","1001","invalid","-1",""};
    const unsigned rates[]={1,60,144,1000,144,59,144,60,60,60,60,60};
    for(unsigned i=0;i<sizeof(rates)/sizeof(*rates);++i) {
        assert(!setenv("RCLOL_REFRESH_HZ",values[i],1));
        DGTimingSnapshot s=readTiming(7,0,cache);
        assert(s.rateNumerator==rates[i] && s.rateDenominator==1);
        assert(s.flags==(DG_TIMING_NOMINAL|DG_TIMING_SOFTWARE));
        assert(!s.presentationNS && !s.presentationCounter);
    }
    assert(!unsetenv("RCLOL_REFRESH_HZ"));
}
int main(int argc, char **argv) {
    assert(argc == 3);
    void *a = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    void *b = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
    assert(a && b);
    int (*publish)(const DGTimingSnapshot*) = dlsym(a, "DGTimingPublish");
    uint64_t (*invalidate)(uint64_t) = dlsym(a, "DGTimingInvalidate");
    void (*bind)(uint64_t,uint64_t) = dlsym(a,"DGTimingBind");
    void (*forget)(uint64_t) = dlsym(a,"DGTimingForget");
    DGTimingSnapshot (*readTiming)(uint64_t,uint32_t,DGTimingCache*) = dlsym(b,"DGTimingRead");
    int (*map)(uint64_t,uint64_t,uint64_t,uint64_t,DGTimingClock*,uint64_t*) = dlsym(a,"DGTimingMapClock");
    DGTimingCache cache = {0};
    assert(!unsetenv("RCLOL_REFRESH_HZ"));
    checkOverrides(readTiming,&cache); /* Also works before any backend publishes. */
    DGTimingSnapshot s = readTiming(7,0,&cache);
    assert(s.flags & DG_TIMING_SOFTWARE);
    s = (DGTimingSnapshot){.target=99,.outputID=401,.displayID=2,
        .rateNumerator=60000,.rateDenominator=1001,.flags=DG_TIMING_NOMINAL};
    s.generation = invalidate(99);
    assert(s.generation && publish(&s));
    bind(7,99);
    s = readTiming(7,0,&cache);
    assert(s.outputID==401 && s.rateNumerator==60000 && s.rateDenominator==1001);
    s.flags |= DG_TIMING_FEEDBACK|DG_TIMING_FIXED|DG_TIMING_COUNTER;
    s.presentationNS=1000000000; s.presentationCounter=40;
    assert(publish(&s));
    s=readTiming(7,0,&cache);
    assert(s.flags & DG_TIMING_FEEDBACK);
    checkOverrides(readTiming,&cache); /* A cached backend cannot override the setting. */
    s=readTiming(7,0,&cache);
    assert(s.rateNumerator==60000 && s.rateDenominator==1001 && (s.flags&DG_TIMING_FEEDBACK));
    uint64_t old=s.generation;
    assert(invalidate(99)>old);
    assert(!publish(&s)); /* a late observation cannot revive an old surface */
    s=readTiming(7,0,&cache);
    assert(!(s.flags & DG_TIMING_FEEDBACK));
    s=(DGTimingSnapshot){.target=99,.generation=invalidate(99),.outputID=402,
        .rateNumerator=144,.rateDenominator=1,.flags=DG_TIMING_NOMINAL};
    assert(publish(&s));
    s=readTiming(7,0,&cache); assert(s.outputID==402);
    assert(forget);
    for (uint64_t i=1000;i<1400;++i) {
        DGTimingSnapshot next={.target=i,.generation=invalidate(i),.outputID=i,
            .rateNumerator=144,.rateDenominator=1,.flags=DG_TIMING_NOMINAL};
        assert(next.generation && publish(&next));
        bind(i,i);DGTimingCache fresh={0};
        assert(readTiming(i,0,&fresh).outputID==i);
        bind(i,0);forget(i);
        assert(!publish(&next));
    }
    s=readTiming(7,0,&cache); assert(s.outputID==402);
    DGTimingClock clock={0}; uint64_t ns=0;
    assert(map(5000,10000,20000,10010,&clock,&ns)); assert(ns==14995);
    assert(!map(5000,10000,20000,9000,&clock,&ns));
    assert(!map(5000,10000,20000,1000000,&clock,&ns));
    clock=(DGTimingClock){0};
    assert(map(5000000000,5000000100,6000000100,5000000120,&clock,&ns));
    assert(!map(5000000200,5000000300,7000000300,5000000320,&clock,&ns));
    /* Both loaded instances share the same process-scoped file. Cleanup is owned. */
    char path[256]; snprintf(path,sizeof path,"/dev/shm/darling-display-timing-v1-%u-%u.state",(unsigned)getuid(),(unsigned)getpid());
    int fd=open(path,O_RDWR); assert(fd>=0);
    assert(flock(fd,LOCK_EX|LOCK_NB)==0);
    /* A coherent same-epoch cache survives contention without waiting. */
    s=readTiming(7,0,&cache); assert(s.outputID==402);
    /* An interrupted publication invalidates cached feedback before the lock releases. */
    uint64_t epoch=cache.epoch+1;
    assert(pwrite(fd,&epoch,sizeof epoch,32)==sizeof epoch);
    s=readTiming(7,0,&cache); assert(s.flags & DG_TIMING_SOFTWARE);
    close(fd); unlink(path);
    dlclose(b); dlclose(a);
}
''')
            for name in ('one.so', 'two.so'):
                subprocess.run(['cc', '-std=c11', '-D_GNU_SOURCE', '-DDG_TIMING_HOSTED',
                                '-shared', '-fPIC', '-I', str(root), str(root / 'provider.c'),
                                '-o', str(root / name)], check=True, capture_output=True)
            subprocess.run(['cc', '-std=c11', '-D_GNU_SOURCE', '-I', str(root),
                            str(root / 'probe.c'), '-ldl', '-o', str(root / 'probe')],
                           check=True, capture_output=True)
            subprocess.run([str(root / 'probe'), str(root / 'one.so'), str(root / 'two.so')],
                           check=True, timeout=10)
