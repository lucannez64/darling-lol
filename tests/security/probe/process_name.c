// Prints, as JSON, every name macOS reports for this process and its parent:
// sysctl KERN_PROC_PID p_comm, proc_pidinfo PROC_PIDT_SHORTBSDINFO pbsi_comm,
// PROC_PIDTBSDINFO pbi_comm / pbi_name and libproc proc_name(). Anti-tamper code
// (MVG) reports the parent's name; Darling used to answer "mldr" for every process.
#include <libproc.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <string.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <unistd.h>

extern char** environ;

static void print_string(const char* key, const char* value, int last)
{
	printf("    \"%s\": \"", key);
	for (const char* p = value; *p; ++p) {
		if (*p == '"' || *p == '\\')
			putchar('\\');
		putchar(*p);
	}
	printf("\"%s\n", last ? "" : ",");
}

static void report(const char* label, pid_t pid, int last)
{
	struct kinfo_proc kinfo;
	size_t size = sizeof(kinfo);
	int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PID, pid };
	char sysctl_comm[MAXCOMLEN + 1] = "";
	if (sysctl(mib, 4, &kinfo, &size, NULL, 0) == 0 && size > 0)
		strlcpy(sysctl_comm, kinfo.kp_proc.p_comm, sizeof(sysctl_comm));

	struct proc_bsdshortinfo shortinfo;
	char short_comm[MAXCOMLEN + 1] = "";
	if (proc_pidinfo(pid, PROC_PIDT_SHORTBSDINFO, 0, &shortinfo, sizeof(shortinfo)) == sizeof(shortinfo))
		strlcpy(short_comm, shortinfo.pbsi_comm, sizeof(short_comm));

	struct proc_bsdinfo bsdinfo;
	char bsd_comm[MAXCOMLEN + 1] = "", bsd_name[2 * MAXCOMLEN + 1] = "";
	if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsdinfo, sizeof(bsdinfo)) == sizeof(bsdinfo)) {
		strlcpy(bsd_comm, bsdinfo.pbi_comm, sizeof(bsd_comm));
		strlcpy(bsd_name, bsdinfo.pbi_name, sizeof(bsd_name));
	}

	char name[2 * MAXCOMLEN + 1] = "";
	proc_name(pid, name, sizeof(name));

	char path[PROC_PIDPATHINFO_MAXSIZE] = "";
	proc_pidpath(pid, path, sizeof(path));

	printf("  \"%s\": {\n", label);
	printf("    \"pid\": %d,\n", pid);
	print_string("sysctl_p_comm", sysctl_comm, 0);
	print_string("pbsi_comm", short_comm, 0);
	print_string("pbi_comm", bsd_comm, 0);
	print_string("pbi_name", bsd_name, 0);
	print_string("proc_name", name, 0);
	print_string("path", path, 1);
	printf("  }%s\n", last ? "" : ",");
}

// With no argument: report, then spawn this executable again with "child" and wait,
// so the child reports a Mach-O parent the way LeagueClient sees RiotClientServices.
int main(int argc, char** argv)
{
	printf("{\n");
	report("self", getpid(), 0);
	report("parent", getppid(), 1);
	printf("}\n");
	fflush(stdout);

	if (argc > 1)
		return 0;
	char* child_argv[] = { argv[0], "child", NULL };
	pid_t child;
	if (posix_spawn(&child, argv[0], NULL, NULL, child_argv, environ) != 0)
		return 1;
	int status = 0;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
		return 1;
	return 0;
}
