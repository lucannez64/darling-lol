/*
 * Guest-side M5 test: the signed __TEXT/__LINKEDIT ranges of the main image
 * and of a dlopen()ed dylib must reject mprotect(PROT_WRITE).
 */

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach-o/loader.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

static int probe_function(void) {
	return 1;
}

static int sealed(void* address, const char* label) {
	uintptr_t page = (uintptr_t) address & ~(uintptr_t)(getpagesize() - 1);
	void* mapped;
	int result;

	errno = 0;
	result = mprotect((void*) page, getpagesize(), PROT_READ | PROT_WRITE | PROT_EXEC);
	printf("%s mprotect r=%d errno=%d\n", label, result, errno);
	if (result != -1)
		return 0;

	/* mseal also rejects a fixed mapping over the sealed range. */
	errno = 0;
	mapped = mmap((void*) page, getpagesize(), PROT_READ | PROT_WRITE,
	    MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
	printf("%s mmap_fixed r=%s errno=%d\n", label, mapped == MAP_FAILED ? "failed" : "ok", errno);
	return mapped == MAP_FAILED;
}

/* Mutate the original file after loading. A mapping of verified, sealed
 * snapshot bytes must stay unchanged even though pwrite to the file succeeds. */
static int backing_write(void* symbol, const char* path, const char* label) {
    Dl_info info;
    if (!dladdr(symbol, &info)) return 0;
    const struct mach_header_64* header = info.dli_fbase;
    if (header->magic != MH_MAGIC_64) return 0;
    const struct load_command* command = (const void*)(header + 1);
    intptr_t slide = 0;
    int found_base = 0;
    for (uint32_t i = 0; i < header->ncmds; i++) {
        if (command->cmd == LC_SEGMENT_64) {
            const struct segment_command_64* segment = (const void*)command;
            if (segment->fileoff == 0 && segment->filesize) {
                slide = (intptr_t)header - segment->vmaddr;
                found_base = 1;
                break;
            }
        }
        command = (const void*)((const char*)command + command->cmdsize);
    }
    if (!found_base) return 0;
    command = (const void*)(header + 1);
    for (uint32_t i = 0; i < header->ncmds; i++) {
        if (command->cmd == LC_SEGMENT_64) {
            const struct segment_command_64* segment = (const void*)command;
            uintptr_t start = segment->vmaddr + slide;
            if ((uintptr_t)symbol >= start && (uintptr_t)symbol - start < segment->filesize) {
                off_t offset = segment->fileoff + (uintptr_t)symbol - start;
                unsigned char original, modified;
                int fd = open(path, O_RDWR);
                if (fd < 0) return 0;
                if (pread(fd, &original, 1, offset) != 1) { close(fd); return 0; }
                modified = original ^ 1;
                if (pwrite(fd, &modified, 1, offset) != 1) { close(fd); return 0; }
                int unchanged = *(volatile unsigned char*)symbol == original;
                int restored = pwrite(fd, &original, 1, offset) == 1;
                close(fd);
                printf("%s backing_write unchanged=%d restored=%d\n", label, unchanged, restored);
                return unchanged && restored;
            }
        }
        command = (const void*)((const char*)command + command->cmdsize);
    }
    return 0;
}

int main(int argc, char** argv) {
	void* handle;
	void* symbol;

	if (!backing_write((void*) &probe_function, argv[0], "main"))
		return 1;
	if (!sealed((void*) &probe_function, "main"))
		return 1;
	if (argc < 2) {
		printf("SEAL_OK\n");
		return 0;
	}

	handle = dlopen(argv[1], RTLD_NOW);
	if (handle == 0) {
		printf("DLOPEN_FAIL %s\n", dlerror());
		return 1;
	}
	symbol = dlsym(handle, "darling_lv_target");
	if (symbol == 0) {
		printf("DLSYM_FAIL\n");
		return 1;
	}
	if (!backing_write(symbol, argv[1], "library"))
		return 1;
	if (!sealed(symbol, "library"))
		return 1;

	printf("SEAL_OK\n");
	return 0;
}
