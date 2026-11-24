/* Native MIT-SHM transport. Server-created fd segments cross Darling's IPC
 * namespace; classic SysV XShmAttach cannot. Keep all native fds in Linux. */
#include <X11/Xlib-xcb.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>
#include <X11/extensions/shape.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <xcb/shm.h>
#include <stdio.h>
#include "../build/darling-src/src/frameworks/OpenGL/include/OpenGL/DarlingNativePresent.h"
typedef struct {
  Display *display, *ownerDisplay;
  XImage *image;
  GC privateGC;
  XShmSegmentInfo info;
  size_t size;
  unsigned width, height;
  uint32_t visualID;
  pthread_mutex_t control, render;
  pthread_t worker;
  int started, running;
  Drawable target;
  Window parent, gpuWindow;
  Colormap gpuColormap;
  int gpuMapped;
  GC gc;
  char path[128];
  DarlingNativePresentTarget *gpuTarget;
  char gpuTargetPath[128];
} Frame;
typedef struct {
  uint32_t magic, sequence, width, height, stride, format;
  uint64_t size;
} Header;
__attribute__((visibility("default"))) void *
DarlingXShmCreate(Display *d, Visual *v, unsigned depth, unsigned width,
                  unsigned height) {
  if (!d || !width || !height)
    return NULL;
  Display *transport = XOpenDisplay(DisplayString(d));
  if (!transport)
    return NULL;
  if (!XShmQueryExtension(transport)) {
    XCloseDisplay(transport);
    return NULL;
  }
  XVisualInfo query = {.visualid = v->visualid};
  int count = 0;
  XVisualInfo *visual = XGetVisualInfo(transport, VisualIDMask, &query, &count);
  if (!visual) {
    XCloseDisplay(transport);
    return NULL;
  }
  Frame *f = calloc(1, sizeof(*f));
  if (!f) {
    XFree(visual);
    XCloseDisplay(transport);
    return NULL;
  }
  f->display = transport;
  f->ownerDisplay = d;
  f->visualID=XVisualIDFromVisual(v);
  f->width = width;
  f->height = height;
  f->image = XShmCreateImage(transport, visual->visual, depth, ZPixmap, NULL,
                             &f->info, width, height);
  XFree(visual);
  if (!f->image) {
    XCloseDisplay(transport);
    free(f);
    return NULL;
  }
  if (f->image->bits_per_pixel != 32 ||
      (unsigned)f->image->bytes_per_line < width * 4) {
    XDestroyImage(f->image);
    XCloseDisplay(transport);
    free(f);
    return NULL;
  }
  f->size = (size_t)f->image->bytes_per_line * height;
  xcb_connection_t *c = XGetXCBConnection(transport);
  xcb_shm_seg_t segment = xcb_generate_id(c);
  xcb_generic_error_t *error = NULL;
  xcb_shm_create_segment_cookie_t cookie =
      xcb_shm_create_segment(c, segment, f->size, 0);
  xcb_shm_create_segment_reply_t *reply =
      xcb_shm_create_segment_reply(c, cookie, &error);
  if (!reply || error || reply->nfd != 1) {
    free(error);
    free(reply);
    XDestroyImage(f->image);
    XCloseDisplay(transport);
    free(f);
    return NULL;
  }
  int fd = xcb_shm_create_segment_reply_fds(c, reply)[0];
  void *memory = mmap(NULL, f->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  free(reply);
  if (memory == MAP_FAILED) {
    xcb_shm_detach(c, segment);
    xcb_flush(c);
    XDestroyImage(f->image);
    XCloseDisplay(transport);
    free(f);
    return NULL;
  }
  f->info.shmseg = segment;
  f->info.shmaddr = memory;
  f->info.readOnly = False;
  f->image->data = memory;
  pthread_mutex_init(&f->control, NULL);
  pthread_mutex_init(&f->render, NULL);
  return f;
}
static int drawFrame(Frame *f, Drawable target, GC gc, const void *pixels,
                     size_t stride) {
  if (!f || !pixels || stride < f->width * 4)
    return 0;
  pthread_mutex_lock(&f->render);
  if (!f->privateGC)
    f->privateGC = XCreateGC(f->display, target, 0, NULL);
  (void)gc;
  for (unsigned y = 0; y < f->height; y++)
    memcpy(f->image->data + y * f->image->bytes_per_line,
           (const char *)pixels + y * stride, f->width * 4);
  int result = XShmPutImage(f->display, target, f->privateGC, f->image, 0, 0, 0,
                            0, f->width, f->height, False);
  /* Wait for server consumption before reusing the segment on the next tick. */
  XSync(f->display, False);
  pthread_mutex_unlock(&f->render);
  return result;
}
__attribute__((visibility("default"))) int
DarlingXShmPut(void *handle, Drawable target, GC gc, const void *pixels,
               size_t stride) {
  Frame *f = handle;
  if (!f)
    return 0;
  if (f->started) {
    pthread_mutex_lock(&f->render);
    int result = XShmPutImage(f->display, target, f->privateGC, f->image, 0, 0,
                              0, 0, f->width, f->height, False);
    XSync(f->display, False);
    pthread_mutex_unlock(&f->render);
    return result;
  }
  return drawFrame(f, target, gc, pixels, stride);
}
/* The transport connection is a different X client. Raising through it
 * triggers the owner's SubstructureRedirect handler. Query through the private
 * connection, and use the owner only when a sibling actually covers us. */
static void ensureRaised(Frame *f) {
  Window root, parent, *children = NULL;
  unsigned count = 0;
  if (XQueryTree(f->display, f->parent, &root, &parent, &children, &count)) {
    int covered = count && children[count - 1] != f->target;
    if (children)
      XFree(children);
    if (covered) {
      XRaiseWindow(f->ownerDisplay, f->target);
      XFlush(f->ownerDisplay);
    }
  }
}

static void *pumpFrames(void *arg) {
  Frame *f = arg;
  void *mapping = NULL;
  size_t length = 0;
  char mappedPath[128] = {0};
  uint32_t sequence = 0;
  unsigned char *snapshot = malloc(f->size);
  struct timespec deadline;
  clock_gettime(CLOCK_MONOTONIC, &deadline);
  unsigned maintenanceTick = 0;
  while (snapshot) {
    char path[128];
    pthread_mutex_lock(&f->control);
    int running = f->running;
    memcpy(path, f->path, sizeof(path));
    pthread_mutex_unlock(&f->control);
    if (!running)
      break;
    if (strcmp(path, mappedPath)) {
      int fd = open(path, O_RDONLY);
      struct stat st;
      if (fd >= 0) {
        if (!fstat(fd, &st) && st.st_size >= (off_t)sizeof(Header) &&
            st.st_size <= (off_t)(sizeof(Header) + 64u * 1024u * 1024u)) {
          void *next = mmap(NULL, st.st_size, PROT_READ, MAP_SHARED, fd, 0);
          if (next != MAP_FAILED) {
            if (mapping)
              munmap(mapping, length);
            mapping = next;
            length = st.st_size;
            memcpy(mappedPath, path, sizeof(path));
            sequence = 0;
          }
        }
        close(fd);
      }
    }
    /* Sample the committed header promptly, independently of the producer's
     * frame phase. X11 stacking/exposure maintenance still runs at about60Hz. */
    int maintenance = maintenanceTick++ % 8 == 0;
    int exposed = 0;
    if (maintenance) {
      ensureRaised(f);
      XEvent event;
      while (XCheckWindowEvent(f->display, f->target, ExposureMask, &event))
        exposed = 1;
    }
    uint32_t gpuReady=f->gpuTarget?__atomic_load_n(&f->gpuTarget->ready,__ATOMIC_ACQUIRE):0;
    if(gpuReady==1){if(f->gpuWindow&&!f->gpuMapped){XMapRaised(f->display,f->gpuWindow);XSync(f->display,False);f->gpuMapped=1;}__atomic_store_n(&f->gpuTarget->ready,2,__ATOMIC_RELEASE);gpuReady=2;}
    if(!gpuReady && f->gpuMapped){XUnmapWindow(f->display,f->gpuWindow);XFlush(f->display);f->gpuMapped=0;}
    if (mapping && !gpuReady) {
      Header before, after;
      memcpy(&before, mapping, sizeof(before));
      __sync_synchronize();
      size_t offset =
          before.magic == 0x43414632u && (before.format & 0x40000000u)
              ? 32u * 1024u * 1024u
              : 0;
      if ((before.magic == 0x43414652u || before.magic == 0x43414632u) &&
          !(before.sequence & 1) && before.sequence != sequence &&
          before.width == f->width && before.height == f->height &&
          before.stride >= before.width * 4 &&
          before.size == (uint64_t)before.stride * before.height &&
          offset <= length - sizeof(Header) &&
          before.size <= length - sizeof(Header) - offset) {
        for (unsigned y = 0; y < f->height; y++)
          memcpy(snapshot + y * f->image->bytes_per_line,
                 (char *)mapping + sizeof(Header) + offset +
                     (size_t)y * before.stride,
                 f->width * 4);
        __sync_synchronize();
        memcpy(&after, mapping, sizeof(after));
        if (after.sequence == before.sequence) {
          drawFrame(f, f->target, f->gc, snapshot, f->image->bytes_per_line);
          sequence = before.sequence;
        }
      } else {
        if (exposed)
          DarlingXShmPut(f, f->target, f->gc, NULL, 0);
        else if (maintenance)
          XFlush(f->display);
      }
    }
    /* darling-lol perf: 500µs poll interval (2000 Hz) reduces average frame
     * detection latency from 1ms to 0.25ms at negligible CPU cost. */
    deadline.tv_nsec += 500000;
    if (deadline.tv_nsec >= 1000000000) {
      deadline.tv_nsec -= 1000000000;
      deadline.tv_sec++;
    }
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (now.tv_sec > deadline.tv_sec ||
        (now.tv_sec == deadline.tv_sec && now.tv_nsec > deadline.tv_nsec))
      deadline = now;
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL);
  }
  if (mapping)
    munmap(mapping, length);
  free(snapshot);
  return NULL;
}
__attribute__((visibility("default"))) void
DarlingXShmSetTiming(void *handle, uint32_t numerator, uint32_t denominator) {
  Frame *f=handle;
  if(!f || !f->gpuTarget)return;
  DarlingNativePresentTarget *t=f->gpuTarget;
  uint32_t seq=__atomic_load_n(&t->reserved[0],__ATOMIC_RELAXED);
  seq=(seq+2)|DARLING_NATIVE_TIMING_V2;
  __atomic_store_n(&t->reserved[0],seq|1,__ATOMIC_RELEASE);
  __atomic_store_n(&t->reserved[4],numerator,__ATOMIC_RELAXED);
  __atomic_store_n(&t->reserved[5],denominator,__ATOMIC_RELAXED);
  __atomic_store_n(&t->reserved[0],seq&~1u,__ATOMIC_RELEASE);
}

__attribute__((visibility("default"))) int
DarlingXShmStart(void *handle, Drawable target, GC gc, const char *path) {
  Frame *f = handle;
  (void)gc;
  if (!f || !path || strlen(path) >= sizeof(f->path))
    return 0;
  pthread_mutex_lock(&f->control);
  strcpy(f->path, path);
  pthread_mutex_unlock(&f->control);
  if (f->started)
    return 1;
  Window root, *children = NULL;
  unsigned count = 0;
  if (!XQueryTree(f->display, target, &root, &f->parent, &children, &count))
    return 0;
  if (children)
    XFree(children);
  f->target = target;
  f->gc = f->privateGC;
  f->running = 1;
  XSelectInput(f->display, target, ExposureMask);
  if (f->width >= 256 && f->height >= 256) {
    XVisualInfo gpuVisual;
    if (XMatchVisualInfo(f->display, DefaultScreen(f->display), 32, TrueColor, &gpuVisual)) {
      f->gpuColormap = XCreateColormap(f->display, DefaultRootWindow(f->display), gpuVisual.visual, AllocNone);
      XSetWindowAttributes attrs = {.colormap = f->gpuColormap, .border_pixel = 0, .background_pixmap = None};
      f->gpuWindow = XCreateWindow(f->display, target, 0, 0, f->width, f->height, 0, 32, InputOutput, gpuVisual.visual, CWColormap | CWBorderPixel | CWBackPixmap, &attrs);
      XShapeCombineRectangles(f->display, f->gpuWindow, ShapeInput, 0, 0, NULL, 0, ShapeSet, Unsorted);
      f->visualID = gpuVisual.visualid;
    }
    /* Ensure the X server has created gpuWindow before publishing it to GPU processes. */
    XSync(f->display, False);
    const char *directory = getenv("DARLING_GPU_TARGET_DIRECTORY");
    if (!directory)
      directory = "/dev/shm";
    snprintf(f->gpuTargetPath, sizeof(f->gpuTargetPath), "%s/darling-ca-gpu-target-%u-%u-%u", directory, getuid(), f->width, f->height);
    int fd = open(f->gpuTargetPath, O_CREAT | O_RDWR, 0600);
    if (fd >= 0) {
      if (!ftruncate(fd, sizeof(DarlingNativePresentTarget))) {
        void *p = mmap(NULL, sizeof(DarlingNativePresentTarget), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (p != MAP_FAILED) {
          f->gpuTarget = p;
          __atomic_store_n(&f->gpuTarget->magic, 0, __ATOMIC_RELEASE);
          f->gpuTarget->target = f->gpuWindow ? f->gpuWindow : target;
          f->gpuTarget->width = f->width;
          f->gpuTarget->height = f->height;
          f->gpuTarget->ready = 0;
          f->gpuTarget->presenter = 0;
          f->gpuTarget->sequence = 0;
          f->gpuTarget->visual = f->visualID;
          f->gpuTarget->flags = getenv("DARLING_GPU_SYNTHETIC_TARGET") ? 0x54455354 : 0;
          memset(f->gpuTarget->reserved, 0, sizeof(f->gpuTarget->reserved));
          DarlingXShmSetTiming(f,60,1);
        }
      }
      close(fd);
    }
  } else {
    XFlush(f->display);
  }
  if (pthread_create(&f->worker, NULL, pumpFrames, f)) {
    f->running = 0;
    return 0;
  }
  if (f->gpuTarget)
    __atomic_store_n(&f->gpuTarget->magic, DARLING_NATIVE_PRESENT_MAGIC, __ATOMIC_RELEASE);
  f->started = 1;
  return 1;
}
__attribute__((visibility("default"))) void DarlingXShmDestroy(void *handle) {
  Frame *f = handle;
  if (!f)
    return;
  if (f->started) {
    pthread_mutex_lock(&f->control);
    f->running = 0;
    pthread_mutex_unlock(&f->control);
    pthread_join(f->worker, NULL);
  }
  if(f->gpuTarget){
    if(f->gpuTarget->target==(f->gpuWindow?f->gpuWindow:f->target)){__atomic_store_n(&f->gpuTarget->magic,0,__ATOMIC_RELEASE);unlink(f->gpuTargetPath);}
    munmap(f->gpuTarget,sizeof(DarlingNativePresentTarget));
  }
  if(f->gpuWindow)XDestroyWindow(f->display,f->gpuWindow);
  if(f->gpuColormap)XFreeColormap(f->display,f->gpuColormap);
  pthread_mutex_destroy(&f->control);
  pthread_mutex_destroy(&f->render);
  xcb_connection_t *c = XGetXCBConnection(f->display);
  xcb_shm_detach(c, f->info.shmseg);
  XSync(f->display, False);
  munmap(f->info.shmaddr, f->size);
  f->image->data = NULL;
  XDestroyImage(f->image);
  if (f->privateGC)
    XFreeGC(f->display, f->privateGC);
  XCloseDisplay(f->display);
  free(f);
}
