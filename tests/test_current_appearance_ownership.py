"""Exercise the patched appearance storage with the host Objective-C runtime."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def source():
    patch = (ROOT / 'patches/cocotron.patch').read_text()
    section = patch.split('diff --git a/AppKit/NSAppearance.m b/AppKit/NSAppearance.m', 1)[1]
    section = section.split('\ndiff --git ', 1)[0]
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith(('+', ' ')) and not line.startswith('+++'))


def method(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


@unittest.skipUnless(shutil.which('clang') and shutil.which('cc'),
                     'host Objective-C compiler and runtime required')
class CurrentAppearanceOwnershipTests(unittest.TestCase):
    def test_replacement_scope_and_thread_exit_own_the_current_appearance(self):
        text = source()
        storage = text[text.index('static pthread_key_t'):text.index('@implementation NSAppearance')]
        methods = '\n'.join(method(text, signature) for signature in (
            '+ (NSAppearance *) currentDrawingAppearance',
            '+ (void) setCurrentAppearance:',
            '- (void) performAsCurrentDrawingAppearance:'))
        program = r'''
#include <stddef.h>
extern id class_createInstance(Class, size_t);
extern id object_dispose(id);
#define nil ((id)0)
#include <pthread.h>
#include <assert.h>
static unsigned deallocated;
/* Blocks stay on the caller's stack; only their compiler class markers are
 * needed for direct invocation. The tested methods never copy a block. */
void *_NSConcreteStackBlock[32];
void *_NSConcreteGlobalBlock[32];
__attribute__((objc_root_class))
@interface NSAppearance {
    Class isa;
    unsigned references;
}
+ (id)alloc;
- (id)retain;
- (void)release;
+ (NSAppearance *)currentDrawingAppearance;
+ (void)setCurrentAppearance:(NSAppearance *)appearance;
- (void)performAsCurrentDrawingAppearance:(void (^)(void))block;
@end
''' + storage + r'''
@implementation NSAppearance
+ (id)alloc {
    NSAppearance *value = class_createInstance(self, 0);
    value->references = 1;
    return value;
}
- (id)retain { ++references; return self; }
- (void)release {
    if (--references == 0) { ++deallocated; object_dispose(self); }
}
''' + methods + r'''
@end
static void *worker(void *unused) {
    NSAppearance *value = [NSAppearance alloc];
    [NSAppearance setCurrentAppearance:value];
    [value release];
    return 0;
}
int main(void) {
    NSAppearance *first = [NSAppearance alloc];
    [NSAppearance setCurrentAppearance:first];
    [first release]; /* The caller's autorelease pool no longer owns it. */
    assert(deallocated == 0);
    assert([NSAppearance currentDrawingAppearance] == first);
    NSAppearance *second = [NSAppearance alloc];
    [NSAppearance setCurrentAppearance:second];
    [second release];
    assert(deallocated == 1);
    NSAppearance *scoped = [NSAppearance alloc];
    void *inside = scoped;
    [scoped performAsCurrentDrawingAppearance:^{
        assert([NSAppearance currentDrawingAppearance] == inside);
        [NSAppearance setCurrentAppearance:nil];
    }];
    assert([NSAppearance currentDrawingAppearance] == second);
    @try {
        [scoped performAsCurrentDrawingAppearance:^{ @throw (id)inside; }];
        assert(0 && "scope must propagate the exception");
    } @catch (id exception) {
        assert(exception == inside);
    }
    assert([NSAppearance currentDrawingAppearance] == second);
    [scoped release];
    assert(deallocated == 2);
    [NSAppearance setCurrentAppearance:nil];
    assert(deallocated == 3);
    pthread_t thread;
    assert(pthread_create(&thread, 0, worker, 0) == 0);
    assert(pthread_join(thread, 0) == 0);
    assert(deallocated == 4);
}
'''
        library = subprocess.check_output(['cc', '-print-file-name=libobjc.so'], text=True).strip()
        if not Path(library).is_file():
            self.skipTest('GNU Objective-C runtime required for host ownership probe')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'appearance.m'
            binary = Path(folder) / 'appearance'
            path.write_text(program)
            subprocess.run(['clang', '-fobjc-runtime=gcc', '-fblocks', '-fobjc-exceptions', '-fexceptions',
                            str(path), '-lobjc', '-pthread', '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
