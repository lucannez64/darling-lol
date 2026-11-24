/* Guest entry points for the native X11 transport. */
#include <elfcalls.h>
extern struct elf_calls *_elfcalls;
#ifndef DARLING_XSHM_HOST_LIBRARY
#error DARLING_XSHM_HOST_LIBRARY must name the native transport library
#endif
static void *handle;
__attribute__((constructor)) static void loadTransport(void) {
  handle = _elfcalls->dlopen_fatal(DARLING_XSHM_HOST_LIBRARY);
}
void *DarlingXShmGetCreate(void) {
  return _elfcalls->dlsym_fatal(handle, "DarlingXShmCreate");
}
void *DarlingXShmGetPut(void) {
  return _elfcalls->dlsym_fatal(handle, "DarlingXShmPut");
}
void *DarlingXShmGetDestroy(void) {
  return _elfcalls->dlsym_fatal(handle, "DarlingXShmDestroy");
}

void *DarlingXShmGetStart(void) {
  return _elfcalls->dlsym_fatal(handle, "DarlingXShmStart");
}

void *DarlingXShmGetSetTiming(void) {
  return _elfcalls->dlsym_fatal(handle, "DarlingXShmSetTiming");
}
