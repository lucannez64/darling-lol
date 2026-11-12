/* Guest check: a process's Mach ports die with it even after one of its threads exited.
 *
 * darlingserver used to keep every thread that exited through `checkout` (a
 * thread returning from its start routine) alive, and each one kept its process
 * alive: when the process later died, its receive rights were never destroyed and
 * peers holding send rights never got MACH_NOTIFY_DEAD_NAME. Chromium learns
 * that a child died from that notification, so the League client UX never
 * noticed its GPU helper had gone and waited for it forever on quit.
 *
 * The parent registers a rendezvous port with launchd, posix_spawns itself as the
 * child, receives a send right to the child's own receive port and requests a
 * dead-name notification for it. Once both notifications are registered the
 * parent tells the child to exit. Each case expects NOTE_EXIT for the pid and
 * the dead-name notification within 3 s:
 *   exit   the child just exits
 *   early  the child runs and joins one worker thread first, then exits
 * Prints one line per case and exits 0 when both pass. macOS 26.7.1 passes both.
 */
#include <mach/mach.h>
#include <mach/notify.h>
#include <servers/bootstrap.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/event.h>
#include <sys/wait.h>

extern char **environ;

static mach_port_t new_receive(void)
{
	mach_port_t port = MACH_PORT_NULL;
	mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port);
	return port;
}

static void *short_thread(void *arg) { (void)arg; return NULL; }

static int child(const char *name, const char *mode)
{
	mach_port_t parent, inbox = new_receive();
	if (bootstrap_look_up(bootstrap_port, (char *)name, &parent) != KERN_SUCCESS)
		return 2;
	if (!strcmp(mode, "early")) {
		pthread_t worker;
		pthread_create(&worker, NULL, short_thread, NULL);
		pthread_join(worker, NULL);
	}
	/* Hand the parent a send right to our own receive port; it dies with us. */
	mach_msg_header_t hello = {
		.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND),
		.msgh_size = sizeof hello, .msgh_remote_port = parent, .msgh_local_port = inbox,
	};
	if (mach_msg(&hello, MACH_SEND_MSG, sizeof hello, 0, MACH_PORT_NULL, 0, MACH_PORT_NULL) != MACH_MSG_SUCCESS)
		return 3;
	/* Exit only once the parent has registered for our exit and our port's death:
	 * neither a dead-name request nor EVFILT_PROC reports a death that came first. */
	struct { mach_msg_header_t header; char body[64]; } go;
	mach_msg(&go.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof go, inbox, 5000, MACH_PORT_NULL);
	return 0;
}

/* Reap the child without waiting forever on one that is stuck: the result of
 * the case is already decided. */
static void reap(pid_t pid, int exited)
{
	if (!exited)
		kill(pid, SIGKILL);
	waitpid(pid, NULL, 0);
}

static int run_case(const char *self, const char *mode)
{
	char name[96];
	snprintf(name, sizeof name, "org.darling-lol.thread-exit-port-check.%d.%s", getpid(), mode);
	mach_port_t rendezvous = new_receive(), notify = new_receive(), previous;
	mach_port_insert_right(mach_task_self(), rendezvous, rendezvous, MACH_MSG_TYPE_MAKE_SEND);
	if (bootstrap_register(bootstrap_port, name, rendezvous) != KERN_SUCCESS) {
		printf("%-6s FAIL (bootstrap_register)\n", mode);
		return 1;
	}
	pid_t pid;
	char *argv[] = { (char *)self, "child", name, (char *)mode, NULL };
	if (posix_spawn(&pid, self, NULL, NULL, argv, environ) != 0) {
		printf("%-6s FAIL (posix_spawn)\n", mode);
		return 1;
	}
	struct { mach_msg_header_t header; char body[256]; } in;
	/* A child that fails before saying hello must not hang the check. */
	if (mach_msg(&in.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof in, rendezvous, 3000,
	             MACH_PORT_NULL) != MACH_MSG_SUCCESS) {
		printf("%-6s FAIL (child never reported its port)\n", mode);
		reap(pid, 0);
		return 1;
	}
	mach_port_t child_port = in.header.msgh_remote_port;
	if (mach_port_request_notification(mach_task_self(), child_port, MACH_NOTIFY_DEAD_NAME, 0,
	                                   notify, MACH_MSG_TYPE_MAKE_SEND_ONCE, &previous) != KERN_SUCCESS) {
		printf("%-6s FAIL (dead-name request)\n", mode);
		reap(pid, 0);
		return 1;
	}

	int kq = kqueue();
	struct kevent64_s events[2];
	EV_SET64(&events[0], pid, EVFILT_PROC, EV_ADD | EV_ONESHOT, NOTE_EXIT, 0, 0, 0, 0);
	EV_SET64(&events[1], notify, EVFILT_MACHPORT, EV_ADD | EV_ONESHOT, 0, 0, 0, 0, 0);
	if (kevent64(kq, events, 2, NULL, 0, 0, NULL) != 0) {
		printf("%-6s FAIL (kevent registration)\n", mode);
		close(kq);
		reap(pid, 0);
		return 1;
	}
	/* Both notifications are registered: let the child exit. */
	mach_msg_header_t go = {
		.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0),
		.msgh_size = sizeof go, .msgh_remote_port = child_port,
	};
	mach_msg(&go, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof go, 0, MACH_PORT_NULL, 1000, MACH_PORT_NULL);

	int exited = 0, dead_name = 0;
	struct timespec timeout = { 3, 0 };
	while (!(exited && dead_name)) {
		struct kevent64_s got;
		if (kevent64(kq, NULL, 0, &got, 1, 0, &timeout) != 1)
			break;
		if (got.filter == EVFILT_PROC)
			exited = 1;
		if (got.filter == EVFILT_MACHPORT) {
			mach_msg(&in.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof in, notify, 0, MACH_PORT_NULL);
			dead_name = in.header.msgh_id == MACH_NOTIFY_DEAD_NAME;
		}
	}
	close(kq);
	reap(pid, exited);
	int ok = exited && dead_name;
	printf("%-6s %s (NOTE_EXIT %s, dead-name %s)\n", mode, ok ? "PASS" : "FAIL",
	       exited ? "yes" : "no", dead_name ? "yes" : "no");
	return !ok;
}

int main(int argc, char **argv)
{
	if (argc == 4 && !strcmp(argv[1], "child"))
		return child(argv[2], argv[3]);
	setvbuf(stdout, NULL, _IONBF, 0);
	int failures = run_case(argv[0], "exit") + run_case(argv[0], "early");
	return failures != 0;
}
