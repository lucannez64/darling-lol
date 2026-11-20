/* Guest entry point for the experimental native Wayland transport (RCLOL_WAYLAND=1). Loads the host
 * shim and hands the guest its function table; see darling-wayland-api.h. A missing host library must
 * not stop the game, so the non-fatal dlopen is used and DarlingWaylandGetAPI() returns NULL instead. */
#include <elfcalls.h>
#include "darling-wayland-api.h"
extern struct elf_calls *_elfcalls;
#ifndef DARLING_WAYLAND_HOST_LIBRARY
#error DARLING_WAYLAND_HOST_LIBRARY must name the native Wayland shim
#endif
static void *handle;
__attribute__((constructor)) static void loadShim(void) {
  handle = _elfcalls->dlopen(DARLING_WAYLAND_HOST_LIBRARY);
}
const struct dwl_api *DarlingWaylandGetAPI(void) {
  if (!handle) return 0;
  const struct dwl_api *(*getAPI)(void) = _elfcalls->dlsym(handle, "DarlingWaylandAPI");
  return getAPI ? getAPI() : 0;
}
