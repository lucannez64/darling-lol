#include <darlingserver/uring.hpp>
#include <linux/time_types.h>
#include <chrono>
#include <cerrno>
#include <cstdio>

int main() {
    auto ring = DarlingServer::Ring::create(8);
    if (!ring) {
        std::perror("io_uring unavailable");
        return 77;
    }
    __kernel_timespec delay = {0, 10000000};
    auto sqe = ring->nextSubmission();
    sqe->opcode = IORING_OP_TIMEOUT;
    sqe->addr = reinterpret_cast<uint64_t>(&delay);
    sqe->len = 1;
    sqe->user_data = 4;
    auto start = std::chrono::steady_clock::now();
    if (ring->submitAndWait(0) < 0) return 1;
    DarlingServer::Ring::Completion completion;
    if (ring->nextCompletion(completion)) return 2;
    if (ring->submitAndWait(1) < 0) return 3;
    auto elapsed = std::chrono::steady_clock::now() - start;
    if (!ring->nextCompletion(completion)) return 4;
    if (completion.userData != 4 || completion.result != -ETIME) return 5;
    if (elapsed < std::chrono::milliseconds(8)) return 6;
    // A completed kernelSync pass must be able to return with no pending I/O.
    if (ring->submitAndWait(0) < 0) return 7;
    return 0;
}
