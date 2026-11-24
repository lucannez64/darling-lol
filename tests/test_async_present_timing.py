"""The shipped async queue must not overwrite its target's nominal timing."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_runtime_compatibility as runtime


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class AsyncPresentTimingTests(unittest.TestCase):
    def test_enqueue_keeps_versioned_nominal_metadata_on_all_paths(self):
        patched = runtime.CompatibilityChecks().patched
        source = patched('darling', 'src/frameworks/OpenGL/CGLAsyncPresent.inc')
        header = patched('darling', 'src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h')
        setter_source = (runtime.ROOT / 'tools/m58f299-xshm.c').read_text()
        setter = runtime.function(setter_source.replace(
            '__attribute__((visibility("default"))) void\n', 'void '), 'DarlingXShmSetTiming')
        start = source.index('struct DarlingAsyncPresent {')
        end = source.index('\n};', start) + 3
        fixture = header + r'''
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned GLuint,GLenum;
typedef int GLint,GLboolean;
typedef void *GLsync;
struct DarlingAsyncPresent;
typedef struct Context {
    struct DarlingAsyncPresent *native_async;
    DarlingNativePresentTarget *native_present_target;
} *CGLContextObj;
typedef struct {DarlingNativePresentTarget *gpuTarget;} Frame;
static unsigned nativeUploadCount=27,nativeUploadMicroseconds=991;
static int failCopy,failFence,incomplete;
enum {GL_TEXTURE_BINDING_2D,GL_TEXTURE_2D,GL_RGBA8,GL_RGBA,GL_UNSIGNED_BYTE,
      GL_TIMEOUT_IGNORED,GL_READ_FRAMEBUFFER_BINDING,GL_DRAW_FRAMEBUFFER_BINDING,
      GL_SCISSOR_TEST,GL_FRAMEBUFFER_SRGB,GL_READ_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,
      GL_DRAW_FRAMEBUFFER,GL_COLOR_BUFFER_BIT,GL_NEAREST,GL_SYNC_GPU_COMMANDS_COMPLETE,
      GL_FRAMEBUFFER_COMPLETE,GL_NO_ERROR};
#define glGetIntegerv(name,p) (*(p)=0)
#define glGenFramebuffers(n,p) ((void)0)
#define glGenTextures(n,p) ((void)0)
#define glBindTexture(...) ((void)0)
#define glTexImage2D(...) ((void)0)
#define glWaitSync(...) ((void)0)
#define glDeleteSync(...) ((void)0)
#define glIsEnabled(...) 0
#define glDisable(...) ((void)0)
#define glEnable(...) ((void)0)
#define glBindFramebuffer(...) ((void)0)
#define glFramebufferTexture2D(...) ((void)0)
#define glReadBuffer(...) ((void)0)
#define glDrawBuffer(...) ((void)0)
#define glBlitFramebuffer(...) ((void)0)
#define glFlush() ((void)0)
#define glGetError() (failCopy?99:GL_NO_ERROR)
#define glCheckFramebufferStatus(...) (incomplete?99:GL_FRAMEBUFFER_COMPLETE)
#define glFenceSync(...) (failFence?NULL:(GLsync)(uintptr_t)1)
static void stopAsyncPresent(CGLContextObj ctx){ctx->native_async=NULL;}
static void *asyncPresentWorker(void *unused){(void)unused;return NULL;}
''' + source[start:end] + '\n' + setter + '\n' + runtime.function(source, 'enqueueAsyncPresent') + r'''
static void unchanged(DarlingNativePresentTarget *actual,const DarlingNativePresentTarget *expected,uint64_t interval){
    assert(!memcmp(actual,expected,sizeof(*actual)));
    assert(DarlingNativeNominalInterval(actual)==interval);
}
int main(void){
    _Static_assert(sizeof(DarlingNativePresentTarget)==64,"metadata ABI");
    for(int rate=0;rate<2;++rate){
        DarlingNativePresentTarget target={0};Frame frame={&target};
        DarlingXShmSetTiming(&frame,rate?144:60000,rate?1:1001);
        uint64_t interval=rate?6944444:16683333;
        DarlingNativePresentTarget expected=target;
        struct DarlingAsyncPresent queue={0};
        queue.width=640;queue.height=480;queue.running=1;queue.displayed=-1;
        pthread_mutex_init(&queue.mutex,NULL);
        struct Context ctx={&queue,&target};
        for(unsigned n=0;n<4;++n){
            assert(enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480)==1);
            assert(queue.submissions==n+1);
            unchanged(&target,&expected,interval);
        }
        assert(queue.serial[0]==1 && queue.serial[1]==2 && queue.serial[2]==4);
        queue.state[0]=2;queue.state[1]=3;queue.state[2]=2;
        assert(!enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        assert(queue.submissions==4);unchanged(&target,&expected,interval);
        queue.state[0]=0;failCopy=1;
        assert(!enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        assert(queue.state[0]==0 && queue.submissions==4);unchanged(&target,&expected,interval);
        failCopy=0;failFence=1;
        assert(!enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        unchanged(&target,&expected,interval);failFence=0;
        incomplete=1;
        assert(!enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        unchanged(&target,&expected,interval);incomplete=0;
        queue.failed=1;
        assert(!enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        unchanged(&target,&expected,interval);queue.failed=0;
        /* A writer-in-progress sequence stays odd; enqueuing must not turn it
         * into a seemingly coherent timing publication. */
        target.reserved[0]|=1;expected=target;
        assert(enqueueAsyncPresent(&ctx,7,GL_TEXTURE_2D,640,480));
        unchanged(&target,&expected,16666667);
        pthread_mutex_destroy(&queue.mutex);
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probe.c').write_text(fixture)
            result = subprocess.run(['cc', '-std=c11', '-pthread', str(root / 'probe.c'),
                                     '-o', str(root / 'probe')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(root / 'probe')], check=True, timeout=10)
