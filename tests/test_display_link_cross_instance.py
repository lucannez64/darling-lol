"""Run shipped lifecycle locks through two independently loaded framework views."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class DisplayLinkCrossInstanceTests(unittest.TestCase):
    def test_callback_stop_release_can_enter_a_second_instance(self):
        patched = runtime.CompatibilityChecks().patched
        source = patched('darling', 'src/frameworks/CoreVideo/src/CVDisplayLink.mm')
        header = patched('cocotron', 'CoreGraphics/include/CoreGraphics/DarlingDisplayTiming.h')
        record = source[source.index('struct CVLinkStateRec {'):source.index('static const uint32_t kStateMagic')]
        guards = source[source.index('static __thread int callbackClockFD'):
                        source.index('static bool readState(')]
        bodies = ''.join(runtime.function(source, name) for name in ('readHeader', 'slotOffset'))
        bodies += guards
        bodies += ''.join(runtime.function(source, name) for name in ('readState', 'writeState', 'CVDisplayLinkStop'))
        bodies += runtime.function(source, 'CVDisplayLinkRelease').replace(
            '[(NSMutableDictionary*) displayLink release];', 'releaseObject(displayLink);')
        library = header + r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/syscall.h>
#include <unistd.h>
using CVDisplayLinkRef=void*;using CVReturn=int;
constexpr int kCVReturnSuccess=0,kCVReturnInvalidArgument=-1,kCVReturnError=-2;
''' + record + r'''
static const uint32_t kStateMagic=0xc81dfa10;
static int releases;
static void releaseObject(void*){++releases;}
static uint64_t clockThreadID(){return syscall(SYS_gettid);}
static int clockOpen(){return open(getenv("CV_FIXTURE_FILE"),O_CREAT|O_RDWR,0600);}
static void clockClose(int fd){close(fd);}
static long clockRead(int fd,void* p,size_t n,off_t at){return pread(fd,p,n,at);}
static long clockWrite(int fd,const void* p,size_t n,off_t at){return pwrite(fd,p,n,at);}
static int clockFlock(int fd,int op){return flock(fd,op);}
''' + bodies + r'''
extern "C" void setup(){
    int fd=clockOpen();assert(fd>=0);
    CVLinkStateRec header={};header.magic=kStateMagic;header.version=3;header.refs=1;
    assert(clockWrite(fd,&header,sizeof header,0)==sizeof header);
    CVLinkStateRec state={};state.link=(void*)1;state.refs=state.alive=state.running=1;
    assert(clockWrite(fd,&state,sizeof state,slotOffset(0))==sizeof state);
    clockClose(fd);
}
extern "C" void trigger(void(*callback)()){
    CVStateGuard guard;assert(guard.fd>=0 && guard.locked);
    {CVCallbackScope scope(guard.fd);callback();}
    assert(callbackClockFD==-1);
}
extern "C" int stop(void* link){return CVDisplayLinkStop(link);}
extern "C" void release(void* link){CVDisplayLinkRelease(link);}
extern "C" bool alive(){CVStateGuard guard;CVLinkStateRec state;return readState(guard.fd,&state,(void*)1);}
'''
        harness = r'''
#include <cassert>
#include <atomic>
#include <chrono>
#include <dlfcn.h>
#include <thread>
static int(*stopOther)(void*);
static void(*releaseOther)(void*);
static std::thread stopper;
static std::atomic<bool> attempted{false},finished{false};
static void reenter(){assert(stopOther((void*)1)==0);releaseOther((void*)1);}
static void external(){
    stopper=std::thread([]{attempted=true;assert(stopOther((void*)1)==0);finished=true;});
    while(!attempted.load())std::this_thread::yield();
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    assert(!finished.load());
}
int main(int argc,char** argv){
    assert(argc==3);
    void* a=dlopen(argv[1],RTLD_LOCAL|RTLD_NOW);void* b=dlopen(argv[2],RTLD_LOCAL|RTLD_NOW);assert(a&&b);
    auto setup=(void(*)())dlsym(a,"setup");auto trigger=(void(*)(void(*)()))dlsym(a,"trigger");
    auto alive=(bool(*)())dlsym(a,"alive");
    stopOther=(int(*)(void*))dlsym(b,"stop");releaseOther=(void(*)(void*))dlsym(b,"release");
    setup();trigger(reenter);assert(!alive());
    setup();trigger(external);stopper.join();assert(finished.load());
    releaseOther((void*)1);assert(!alive());dlclose(b);dlclose(a);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'library.cpp').write_text(library)
            (root / 'main.cpp').write_text(harness)
            for name in ('one.so', 'two.so'):
                result = subprocess.run(['c++', '-std=c++17', '-shared', '-fPIC', '-pthread',
                                         str(root / 'library.cpp'), '-o', str(root / name)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run(['c++', '-std=c++17', '-pthread', str(root / 'main.cpp'), '-ldl',
                            '-o', str(root / 'probe')], check=True, capture_output=True)
            result = subprocess.run([str(root / 'probe'), str(root / 'one.so'), str(root / 'two.so')],
                                    env={**os.environ, 'CV_FIXTURE_FILE': str(root / 'state')},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
