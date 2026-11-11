#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

/* Chromium 150's WORK_AROUND_CRBUG_40064248 (net/socket/socket_apple.h)
 * treats a nonzero secondary syscall return register (%rdx on x86_64) after a
 * successful sendto/send/write as proof of the crbug: the kernel took the
 * EJUSTRETURN path and never wrote the byte count. It then reports
 * ERR_NETWORK_CHANGED (-21), which is exactly what Riot Client 142 logs for
 * its local UI load and every TLS handshake.
 *
 * This probe reports both halves of the return so we can see whether Darwin's
 * bsd_syscall.S clears %rdx as XNU does.
 */

struct syscall_return2 {
	long value;
	long secondary;
};

extern struct syscall_return2 capture_sendto(int, const void*, size_t, int,
                                             const void*, unsigned);
extern struct syscall_return2 capture_send(int, const void*, size_t, int);
extern struct syscall_return2 capture_write(int, const void*, size_t);

/* Tail-call the libc function so the caller-visible %rdx is untouched.
 * A two-word struct return uses %rax:%rdx, matching the syscall ABI. */
__asm__(
	".text\n"
	".globl _capture_sendto\n"
	"_capture_sendto:\n\tjmp _sendto\n"
	".globl _capture_send\n"
	"_capture_send:\n\tjmp _send\n"
	".globl _capture_write\n"
	"_capture_write:\n\tjmp _write\n");

static void show(const char* label, struct syscall_return2 r)
{
	if (r.value < 0)
		printf("%s: rv=-1 errno=%d (%s), rdx=0x%lx\n", label, errno,
		       strerror(errno), (unsigned long)r.secondary);
	else
		printf("%s: rv=%ld, rdx=0x%lx%s\n", label, r.value,
		       (unsigned long)r.secondary,
		       r.secondary != 0 ? "  <-- Chromium sees a bogus return" : "");
}

static int tcp_pair(int* client_out)
{
	struct sockaddr_in addr;
	socklen_t len = sizeof(addr);
	int listener = socket(AF_INET, SOCK_STREAM, 0);
	int client, server;

	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
	addr.sin_port = 0;
	if (listener < 0 || bind(listener, (struct sockaddr*)&addr, sizeof(addr)) < 0 ||
	    listen(listener, 1) < 0 ||
	    getsockname(listener, (struct sockaddr*)&addr, &len) < 0) {
		printf("tcp listener setup failed: errno=%d (%s)\n", errno, strerror(errno));
		if (listener >= 0)
			close(listener);
		return -1;
	}
	client = socket(AF_INET, SOCK_STREAM, 0);
	if (client < 0 ||
	    connect(client, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
		printf("tcp connect failed: errno=%d (%s)\n", errno, strerror(errno));
		close(listener);
		return -1;
	}
	server = accept(listener, NULL, NULL);
	close(listener);
	if (server < 0) {
		printf("tcp accept failed: errno=%d (%s)\n", errno, strerror(errno));
		close(client);
		return -1;
	}
	*client_out = client;
	return server;
}

int main(void)
{
	struct syscall_return2 r;
	char buf[16] = "probe";
	int pair[2], client, server;

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("capture sizes: sendto=%zu send=%zu write=%zu (two longs expected 16)\n",
	       sizeof(struct syscall_return2), sizeof(struct syscall_return2),
	       sizeof(struct syscall_return2));

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0) {
		r = capture_sendto(pair[0], buf, 5, 0, NULL, 0);
		show("unix stream sendto(5)", r);
		close(pair[0]);
		close(pair[1]);
	}

	server = tcp_pair(&client);
	if (server >= 0) {
		r = capture_sendto(client, buf, 5, 0, NULL, 0);
		show("tcp sendto(5)", r);
		r = capture_send(client, buf, 5, 0);
		show("tcp send(5)", r);
		r = capture_write(client, buf, 5);
		show("tcp write(5)", r);
		close(client);
		close(server);
	}
	return 0;
}
