/*
 * Guest-side M8 test: unknown AMFI calls fail closed and a non-entitled
 * process cannot attach to another task.
 */

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/codesign.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>

#define CS_OPS_DARLING_TASK_FOR_PID_RPC 0x101
struct dc_cs_debug_probe {
	int32_t pid;
	int32_t name_only;
	uint32_t port;
};

#define PT_ATTACHEXC 14

static void busy(unsigned long count) {
	volatile unsigned long i;
	for (i = 0; i < count; i++) {}
}

static int direct_probe(pid_t child, int name_only) {
	struct dc_cs_debug_probe probe = { child, name_only, 0 };
	int rc;

	errno = 0;
	rc = csops(0, CS_OPS_DARLING_TASK_FOR_PID_RPC, &probe, sizeof(probe));
	printf("direct name=%d rc=%d errno=%d port=%u\n",
	    name_only, rc, errno, probe.port);
	return rc;
}

static int mach_write_probe(pid_t child) {
	mach_port_t task = MACH_PORT_NULL;
	mach_vm_address_t address = 0;
	kern_return_t kr;
	char data[8] = "sip";

	kr = task_for_pid(mach_task_self(), child, &task);
	if (kr != KERN_SUCCESS) {
		printf("TASK_PORT_FAIL kr=%d\n", kr);
		return -1;
	}
	if (seteuid(1000) != 0) {
		printf("SETEUID_FAIL errno=%d\n", errno);
		mach_port_deallocate(mach_task_self(), task);
		return -2;
	}
	kr = mach_vm_allocate(task, &address, 4096, VM_FLAGS_ANYWHERE);
	if (kr != KERN_SUCCESS) {
		printf("VM_ALLOCATE_FAIL kr=%d\n", kr);
		mach_port_deallocate(mach_task_self(), task);
		return -3;
	}
	kr = mach_vm_write(task, address, (vm_offset_t) data, sizeof(data));
	printf("mach_vm_write kr=%d\n", kr);
	mach_vm_deallocate(task, address, 4096);
	mach_port_deallocate(mach_task_self(), task);
	return kr == KERN_SUCCESS ? 0 : 1;
}

int main(int argc, char** argv) {
	pid_t child;
	long result;
	int rc;

	if (argc > 1 && (strcmp(argv[1], "write") == 0 ||
	    strcmp(argv[1], "write-deny") == 0)) {
		int deny = strcmp(argv[1], "write-deny") == 0;
		int write_rc;

		child = fork();
		if (child == 0) {
			busy(400000000UL);
			_exit(0);
		}
		if (child < 0) {
			printf("FORK_FAIL\n");
			return 1;
		}
		busy(30000000UL);
		write_rc = mach_write_probe(child);
		kill(child, SIGKILL);
		if ((!deny && write_rc != 0) || (deny && write_rc <= 0)) {
			printf("WRITE_FAIL\n");
			return 1;
		}
		printf(deny ? "WRITE_DENY\n" : "WRITE_ALLOW\n");
		return 0;
	}

	if (argc > 1 && (strcmp(argv[1], "direct") == 0 ||
	    strcmp(argv[1], "direct-deny") == 0 ||
	    strcmp(argv[1], "direct-name") == 0 ||
	    strcmp(argv[1], "direct-name-deny") == 0)) {
		int deny = strstr(argv[1], "deny") != NULL;
		int name_only = strstr(argv[1], "name") != NULL;
		int direct_rc;

		child = fork();
		if (child == 0) {
			busy(400000000UL);
			_exit(0);
		}
		if (child < 0) {
			printf("FORK_FAIL\n");
			return 1;
		}
		busy(30000000UL);
		if (seteuid(1000) != 0) {
			printf("SETEUID_FAIL errno=%d\n", errno);
			kill(child, SIGKILL);
			return 1;
		}
		direct_rc = direct_probe(child, name_only);
		kill(child, SIGKILL);
		if ((!deny && direct_rc != 0) || (deny && direct_rc == 0)) {
			printf("DIRECT_FAIL\n");
			return 1;
		}
		printf(deny ? "DIRECT_DENY\n" : "DIRECT_ALLOW\n");
		return 0;
	}

	errno = 0;
	result = syscall(381, "AMFI", 1234, (void*) 0);
	printf("unknown_amfi r=%ld errno=%d\n", result, errno);
	if (result != -1 || errno != ENOTSUP) {
		printf("AMFI_FAIL\n");
		return 1;
	}

	child = fork();
	if (child == 0) {
		busy(400000000UL);
		_exit(0);
	}
	if (child < 0) {
		printf("FORK_FAIL\n");
		return 1;
	}
	busy(30000000UL);

	if (seteuid(1000) != 0) {
		printf("SETEUID_FAIL errno=%d\n", errno);
		kill(child, SIGKILL);
		return 1;
	}
	errno = 0;
	rc = ptrace(PT_ATTACHEXC, child, (void*) 0, 0);
	printf("ptrace r=%d errno=%d\n", rc, errno);
	kill(child, SIGKILL);
	if (rc != -1 || errno != EPERM) {
		printf("PTRACE_FAIL\n");
		return 1;
	}

	printf("AMFI_OK\n");
	return 0;
}
