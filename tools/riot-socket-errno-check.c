#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <fcntl.h>

/* Chromium connects non-blocking, waits in poll(), reads SO_ERROR, then
 * sends and receives. ERR_NETWORK_CHANGED (-21) appears for every TLS
 * handshake on the 142 client, and one of this chain's errno values is the
 * likely source, so print each step. */

static void report(const char* label, int err)
{
	printf("%s: errno=%d (%s)\n", label, err, strerror(err));
}

static int connect_to(const char* address, int port)
{
	struct sockaddr_in addr;
	int fd;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons(port);
	inet_pton(AF_INET, address, &addr.sin_addr);
	fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0)
		return -1;
	fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
	if (connect(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
		if (errno != EINPROGRESS) {
			report("nonblocking connect", errno);
			close(fd);
			return -1;
		}
		report("nonblocking connect", EINPROGRESS);
	}
	return fd;
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	int fd = connect_to("1.1.1.1", 443);
	if (fd >= 0) {
		struct pollfd pfd = { fd, POLLOUT, 0 };
		int ready = poll(&pfd, 1, 5000);
		printf("poll POLLOUT: ready=%d revents=0x%x\n", ready, pfd.revents);
		if (ready < 0)
			report("poll", errno);
		int error = -1;
		socklen_t len = sizeof(error);
		if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &len) == 0)
			printf("SO_ERROR=%d (%s)\n", error, strerror(error));
		else
			report("getsockopt SO_ERROR", errno);
		int written = (int)send(fd, "x", 1, 0);
		if (written < 0)
			report("send", errno);
		else
			printf("send: wrote %d\n", written);
		char buf[16];
		ssize_t got = recv(fd, buf, sizeof(buf), MSG_DONTWAIT);
		if (got < 0)
			report("recv", errno);
		else
			printf("recv: got %zd\n", got);
		close(fd);
	}
	return 0;
}
