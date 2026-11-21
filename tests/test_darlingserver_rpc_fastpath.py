"""darlingserver's RPC hot path avoids heap churn and exclusive locks.

Every guest call used to allocate and zero two heap buffers per receive slot
and two per reply, take two exclusive registry write-locks inside
`Registry::registerIfAbsent`, and take a third exclusive thread write-lock plus
copy 120 bytes in `Thread::setAddress`.

This suite extracts `MessageDataBuffer`, `Registry::registerIfAbsent`, and
`Thread::setAddress` directly from `patches/darlingserver.patch`, compiles and
runs C++17 harnesses under ASan+UBSan, TSan, and `-Wl,--wrap=malloc` (including
`AF_UNIX` `socketpair` I/O), and checks source invariants for `Message` inline
control storage, `Message::forReceive`, `Server::_ringArmReceive`, and
`const Address&`. Runs on bare Linux without Darling or `build/darling-src`.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/darlingserver.patch'


def patch_section(path):
    """Raw unified diff section for `path` in darlingserver.patch."""
    sections = PATCH.read_text().split('diff --git ')
    matches = [s for s in sections if s.startswith(f'a/{path} b/{path}\n')]
    if len(matches) != 1:
        raise AssertionError(f'expected one {path} section in darlingserver.patch, found {len(matches)}')
    return matches[0]


def patched(path):
    """The hunks of `path` as the patch leaves them (context and added lines)."""
    return '\n'.join(line[1:] for line in patch_section(path).splitlines()
                     if line[:1] in (' ', '+') and not line.startswith('+++'))


def patched_hunks(path, header_substring):
    """Return a list of new-side texts for every hunk whose `@@` header contains `header_substring`."""
    lines = patch_section(path).splitlines()
    results = []
    for idx, line in enumerate(lines):
        if line.startswith('@@ ') and header_substring in line:
            out = []
            for body_line in lines[idx + 1:]:
                if body_line.startswith('@@ '):
                    break
                if body_line[:1] in (' ', '+'):
                    out.append(body_line[1:])
            results.append('\n'.join(out))
    if not results:
        raise AssertionError(f'hunk {header_substring!r} not found in {path}')
    return results


def patched_hunk(path, header_substring):
    """Return the new-side lines of the first hunk whose `@@` header contains `header_substring`."""
    return patched_hunks(path, header_substring)[0]


def extract_brace_block(text, anchor_pattern):
    """Extract a C++ class/function declaration ending at its matching `}` (plus optional `;`)."""
    match = re.search(anchor_pattern, text, re.M)
    if not match:
        raise AssertionError(f'could not find anchor {anchor_pattern!r} in patch')
    start = match.start()
    brace_pos = text.index('{', match.end() - 1)
    depth = 0
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    i = brace_pos
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ''
        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
        elif in_block_comment:
            if ch == '*' and nxt == '/':
                in_block_comment = False
                i += 1
        elif in_string:
            if ch == '\\':
                i += 1
            elif ch == '"':
                in_string = False
        elif in_char:
            if ch == '\\':
                i += 1
            elif ch == "'":
                in_char = False
        else:
            if ch == '/' and nxt == '/':
                in_line_comment = True
                i += 1
            elif ch == '/' and nxt == '*':
                in_block_comment = True
                i += 1
            elif ch == '"':
                in_string = True
            elif ch == "'":
                in_char = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    if end < len(text) and text[end] == ';':
                        end += 1
                    return text[start:end]
        i += 1
    raise AssertionError(f'unbalanced braces for {anchor_pattern!r}')


def extract_message_data_buffer():
    msg_hpp = patched('internal-include/darlingserver/message.hpp')
    return extract_brace_block(msg_hpp, r'^\tclass MessageDataBuffer\s*\{')


def extract_message_private_header_block():
    msg_hpp = patched('internal-include/darlingserver/message.hpp')
    start = msg_hpp.index('static constexpr size_t inlineControlDescriptors = 4;')
    end = msg_hpp.index('void _initWithOther(Message&&);')
    return msg_hpp[start:end].rstrip()


def extract_message_methods_from_patch():
    """Extract the patched DarlingServer::Message methods directly from darlingserver.patch."""
    msg_cpp = patched('src/message.cpp')

    # 1. serverCredentials + outgoingCredentials + initializeServerCredentials
    cred_ns = msg_cpp[msg_cpp.index('namespace {'):msg_cpp.index('DarlingServer::Address::Address()')]

    # 2. Message::Message(size_t, size_t, ...) from hunks -55 and -83
    ctor_part1 = patched_hunk('src/message.cpp', 'DarlingServer::Address::setRawSize(size_t newRawSize)')
    ctor_part1 = ctor_part1[ctor_part1.index('DarlingServer::Message::Message('):]
    ctor_part1 = ctor_part1[:ctor_part1.rindex('\t\t}\n\t}') + len('\t\t}\n\t}')]

    ctor_hunks = patched_hunks('src/message.cpp', 'DarlingServer::Message::Message(size_t bufferSpace')
    hunk_83 = ctor_hunks[0]
    hunk_97 = ctor_hunks[1]

    ctor_part2_head = hunk_83[:hunk_83.index('DarlingServer::Message::Message(ReceiveTag')]
    ctor_part2_head = ctor_part2_head[ctor_part2_head.index('_header.msg_name = &_socketAddress.raw();'):]
    ctor_full = (
        ctor_part1
        + '\n\tif (descriptorSpace > 0) {\n'
        + '\t\tauto fdHeader = reinterpret_cast<decltype(_controlHeader)>(reinterpret_cast<char*>(_controlHeader) + CMSG_SPACE(sizeof(struct ucred)));\n'
        + '\t\tfdHeader->cmsg_len = CMSG_LEN(sizeof(int) * descriptorSpace);\n'
        + '\t\tfdHeader->cmsg_level = SOL_SOCKET;\n'
        + '\t\tfdHeader->cmsg_type = SCM_RIGHTS;\n'
        + '\t\tfor (size_t i = 0; i < descriptorSpace; ++i) {\n'
        + '\t\t\tconst int fd = -1;\n'
        + '\t\t\tmemcpy(CMSG_DATA(fdHeader) + (sizeof(int) * i), &fd, sizeof(int));\n'
        + '\t\t}\n\t}\n'
        + ctor_part2_head
    )

    # 3. Message::Message(ReceiveTag, ...) + Message::forReceive(...) + Message::_initWithOther
    recv_head = hunk_83[hunk_83.index('DarlingServer::Message::Message(ReceiveTag'):]
    init_full = (
        recv_head
        + '\n\t_header.msg_iov = &_dataDescriptor;\n'
        + '\t_header.msg_iovlen = 1;\n'
        + '\t_header.msg_control = _controlHeader;\n'
        + '\t_header.msg_controllen = controlLen;\n'
        + '\t_header.msg_flags = 0;\n'
        + '\t_dataDescriptor.iov_base = _buffer.data();\n'
        + hunk_97
        + '\n\t_dataDescriptor.iov_base = _buffer.data();\n'
        + '\t_dataDescriptor.iov_len = _buffer.size();\n'
        + '};\n'
    )

    # 4. _cleanupSelf
    cleanup_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_cleanupSelf()')
    cleanup_body = cleanup_hunk[:cleanup_hunk.index('struct cmsghdr* DarlingServer::Message::_credentialsHeader()')]
    cleanup_full = (
        'void DarlingServer::Message::_cleanupSelf() {\n'
        '\tif (_controlHeader) {\n'
        '\t\tauto fdHeader = _descriptorHeader();\n'
        '\t\tif (fdHeader) {\n'
        '\t\t\tfor (size_t i = 0; i < _descriptorSpace(); ++i) {\n'
        '\t\t\t\tint fd = -1;\n'
        '\t\t\t\tmemcpy(&fd, CMSG_DATA(fdHeader) + (sizeof(int) * i), sizeof(int));\n'
        '\t\t\t\tif (fd != -1) {\n'
        '\t\t\t\t\tclose(fd);\n'
        + cleanup_body
        + '\n'
    )

    # 5. _credentialsHeader, _descriptorHeader, _descriptorSpace
    cred_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_credentialsHeader() const')
    cred_full = (
        'struct cmsghdr* DarlingServer::Message::_credentialsHeader() {\n'
        '\treturn const_cast<struct cmsghdr*>(const_cast<const Message*>(this)->_credentialsHeader());\n};\n'
        'const struct cmsghdr* DarlingServer::Message::_credentialsHeader() const {\n'
        + cred_hunk
        + '\n\t\thdr = CMSG_NXTHDR(const_cast<struct msghdr*>(&_header), const_cast<struct cmsghdr*>(hdr));\n'
        '\t}\n\treturn nullptr;\n};\n'
    )

    desc_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_descriptorHeader() const')
    desc_full = (
        'struct cmsghdr* DarlingServer::Message::_descriptorHeader() {\n'
        '\treturn const_cast<struct cmsghdr*>(const_cast<const Message*>(this)->_descriptorHeader());\n};\n'
        'const struct cmsghdr* DarlingServer::Message::_descriptorHeader() const {\n'
        + desc_hunk
        + '\n\t\thdr = CMSG_NXTHDR(const_cast<struct msghdr*>(&_header), const_cast<struct cmsghdr*>(hdr));\n'
        '\t}\n\treturn nullptr;\n};\n'
    )

    space_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_descriptorSpace() const')
    space_body = space_hunk[:space_hunk.index('DarlingServer::Message::~Message()')]
    space_full = (
        'size_t DarlingServer::Message::_descriptorSpace() const {\n'
        '\tauto hdr = _descriptorHeader();\n'
        '\tif (!hdr) {\n'
        + space_body
        + '\n'
    )

    # 6. ~Message, move ctor, operator=, rawHeader, data, address, setAddress
    dtor_move_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::Message(Message&& other)')
    dtor_move_full = (
        'DarlingServer::Message::~Message() {\n\t_cleanupSelf();\n};\n'
        'DarlingServer::Message::Message(Message&& other) {\n\t_initWithOther(std::move(other));\n'
        + dtor_move_hunk
        + '\n'
    )
    accessors_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::rawHeader() const')
    accessors_full = 'const struct msghdr& DarlingServer::Message::rawHeader() const {\n' + accessors_hunk + '\n'

    # 7. replaceDescriptors + copyCredentialsOut
    rep_cred_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::extractDescriptorAtIndex')
    rep_cred_hunk = rep_cred_hunk[rep_cred_hunk.index('void DarlingServer::Message::replaceDescriptors'):]
    rep_cred_full = rep_cred_hunk + '\n\t\treturn false;\n\t}\n};\n'

    # 8. _ensureCredentialsHeader + _ensureDescriptorHeader
    ensure_cred_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_ensureCredentialsHeader()')
    ensure_cred_full = (
        'void DarlingServer::Message::_ensureCredentialsHeader() {\n'
        '\tif (_credentialsHeader()) {\n\t\treturn;\n\t}\n'
        + ensure_cred_hunk[:ensure_cred_hunk.index('void DarlingServer::Message::_ensureDescriptorHeader')]
    )
    ensure_desc_hunk = patched_hunk('src/message.cpp', 'DarlingServer::Message::_ensureDescriptorHeader(size_t newSpace)')
    ensure_desc_full = (
        'void DarlingServer::Message::_ensureDescriptorHeader(size_t newSpace) {\n'
        '\tif (_descriptorSpace() == newSpace) {\n\t\treturn;\n'
        + ensure_desc_hunk[:ensure_desc_hunk.index('pid_t DarlingServer::Message::pid() const')]
    )

    return '\n'.join([
        cred_ns,
        ctor_full,
        init_full,
        cleanup_full,
        cred_full,
        desc_full,
        space_full,
        dtor_move_full,
        accessors_full,
        rep_cred_full,
        ensure_cred_full,
        ensure_desc_full,
    ])


def extract_registry_register_if_absent():
    reg_hpp = patched('internal-include/darlingserver/registry.hpp')
    return extract_brace_block(
        reg_hpp,
        r'^\t\ttemplate<class Factory>\s*\n\t\tstd::shared_ptr<Entry> registerIfAbsent\(NSID nsid, Factory&& entryFactory\)\s*\{',
    )


def extract_thread_set_address():
    thread_cpp = patched('src/thread.cpp')
    return extract_brace_block(thread_cpp, r'^void DarlingServer::Thread::setAddress\(const Address& address\)\s*\{')


HARNESS_CPP = r'''
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <pthread.h>
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <new>
#include <optional>
#include <shared_mutex>
#include <stdexcept>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <vector>

#ifdef USE_WRAP_MALLOC
static thread_local bool g_countAllocs = false;
static thread_local size_t g_allocCount = 0;

extern "C" void* __real_malloc(size_t);
extern "C" void* __wrap_malloc(size_t n) {
    if (g_countAllocs) ++g_allocCount;
    return __real_malloc(n);
}
__attribute__((noinline)) void* operator new(size_t n) {
    if (g_countAllocs) ++g_allocCount;
    if (void* p = __real_malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
__attribute__((noinline)) void* operator new[](size_t n) {
    return ::operator new(n);
}
__attribute__((noinline)) void operator delete(void* p) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete(void* p, size_t) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete[](void* p) noexcept { std::free(p); }
__attribute__((noinline)) void operator delete[](void* p, size_t) noexcept { std::free(p); }
#endif

namespace DarlingServer {
using EternalID = uint64_t;

class Address {
private:
    struct sockaddr_un _address{};
    size_t _size = 0;
public:
    Address() {
        _address.sun_family = AF_UNIX;
        _size = sizeof(_address);
    }
    Address(const struct sockaddr_un& rawAddress, size_t addressSize = sizeof(struct sockaddr_un))
        : _address(rawAddress), _size(addressSize) {}
    struct sockaddr_un& raw() { return _address; }
    const struct sockaddr_un& raw() const { return _address; }
    size_t rawSize() const { return _size; }
    void setRawSize(size_t newRawSize) { _size = newRawSize; }
};

/* EXTRACTED MessageDataBuffer */
__MESSAGE_DATA_BUFFER__

class Message {
private:
    __MESSAGE_PRIVATE_FIELDS__

    void _initWithOther(Message&&);
    void _cleanupSelf();

    struct cmsghdr* _credentialsHeader();
    const struct cmsghdr* _credentialsHeader() const;
    struct cmsghdr* _descriptorHeader();
    const struct cmsghdr* _descriptorHeader() const;
    size_t _descriptorSpace() const;

    void _ensureCredentialsHeader();
    void _ensureDescriptorHeader(size_t descriptorSpace);

public:
    static void initializeServerCredentials();
    static Message forReceive(size_t bufferSpace = 256, size_t descriptorSpace = 4);
    Message(size_t bufferSpace = 256, size_t descriptorSpace = 4, std::function<void()> sendNotificationCallback = nullptr);
    ~Message();

    Message(Message&&);
    Message& operator=(Message&&);

    Message(const Message&) = delete;
    Message& operator=(const Message&) = delete;

    struct msghdr& rawHeader();
    const struct msghdr& rawHeader() const;

    MessageDataBuffer& data();
    const MessageDataBuffer& data() const;

    const Address& address() const;
    void setAddress(const Address& address);

    bool copyCredentialsOut(struct ucred& outputCredentials) const;
    void copyCredentialsIn(const struct ucred& inputCredentials) {
        _ensureCredentialsHeader();
        memcpy(CMSG_DATA(_credentialsHeader()), &inputCredentials, sizeof(inputCredentials));
    }

    pid_t pid() const {
        struct ucred creds{};
        return copyCredentialsOut(creds) ? creds.pid : -1;
    }

    void setPID(pid_t p) {
        _ensureCredentialsHeader();
        struct ucred creds{};
        copyCredentialsOut(creds);
        creds.pid = p;
        copyCredentialsIn(creds);
    }

    std::vector<int> descriptors() const {
        std::vector<int> fds;
        if (auto fdHeader = _descriptorHeader()) {
            for (size_t i = 0; i < _descriptorSpace(); ++i) {
                int fd = -1;
                memcpy(&fd, CMSG_DATA(fdHeader) + (sizeof(int) * i), sizeof(int));
                if (fd != -1) fds.push_back(fd);
            }
        }
        return fds;
    }

    void pushDescriptor(int descriptor) {
        if (auto fdHeader = _descriptorHeader()) {
            for (size_t i = 0; i < _descriptorSpace(); ++i) {
                int fd = -1;
                memcpy(&fd, CMSG_DATA(fdHeader) + (sizeof(int) * i), sizeof(int));
                if (fd != -1) continue;
                memcpy(CMSG_DATA(fdHeader) + (sizeof(int) * i), &descriptor, sizeof(int));
                return;
            }
        }
        auto oldSpace = _descriptorSpace();
        _ensureDescriptorHeader(oldSpace + 1);
        memcpy(CMSG_DATA(_descriptorHeader()) + (sizeof(int) * oldSpace), &descriptor, sizeof(int));
    }

    int extractDescriptorAtIndex(size_t index) {
        if (auto fdHeader = _descriptorHeader()) {
            for (size_t i = 0, present = 0; i < _descriptorSpace(); ++i) {
                int fd = -1;
                memcpy(&fd, CMSG_DATA(fdHeader) + (sizeof(int) * i), sizeof(int));
                if (fd == -1) continue;
                if (present++ != index) continue;
                int tmp = -1;
                memcpy(CMSG_DATA(fdHeader) + (sizeof(int) * i), &tmp, sizeof(int));
                return fd;
            }
        }
        return -1;
    }

    void replaceDescriptors(const std::vector<int>& newDescriptors);
};

template<class Entry>
class Registry {
private:
    std::unordered_map<typename Entry::ID, std::shared_ptr<Entry>> _map;
    std::unordered_map<typename Entry::NSID, std::shared_ptr<Entry>> _nsmap;
    std::unordered_map<EternalID, std::shared_ptr<Entry>> _emap;
    uint64_t _eternalCounter = 0;
    mutable std::shared_mutex _rwlock;
    static thread_local bool _registeringWithLockHeld;

public:
    using ID = typename Entry::ID;
    using NSID = typename Entry::NSID;

/* EXTRACTED Registry::registerIfAbsent */
__REGISTRY_REGISTER_IF_ABSENT__

    std::optional<std::shared_ptr<Entry>> lookupEntryByID(ID id) {
        std::shared_lock lock(_rwlock, std::defer_lock);
        if (!_registeringWithLockHeld) {
            lock.lock();
        }
        auto it = _map.find(id);
        if (it == _map.end()) {
            return std::nullopt;
        }
        return it->second;
    }

    std::optional<std::shared_ptr<Entry>> lookupEntryByNSID(NSID nsid) {
        std::shared_lock lock(_rwlock, std::defer_lock);
        if (!_registeringWithLockHeld) {
            lock.lock();
        }
        auto it = _nsmap.find(nsid);
        if (it == _nsmap.end()) {
            return std::nullopt;
        }
        return it->second;
    }
};

class Thread {
private:
    Address _address;
    mutable std::shared_mutex _rwlock;
public:
    Address address() const {
        std::shared_lock lock(_rwlock);
        return _address;
    }
    void setAddress(const Address& address);
};
} // namespace DarlingServer

template<class T>
thread_local bool DarlingServer::Registry<T>::_registeringWithLockHeld = false;

/* EXTRACTED Thread::setAddress */
__THREAD_SET_ADDRESS__

/* EXTRACTED Message methods */
__MESSAGE_METHODS__

using DarlingServer::Address;
using DarlingServer::Message;
using DarlingServer::MessageDataBuffer;
using DarlingServer::Registry;
using DarlingServer::Thread;

static void require(bool cond, const char* msg) {
    if (!cond) throw std::runtime_error(msg);
}

static unsigned openFDs() {
    DIR* dir = opendir("/proc/self/fd");
    require(dir, "opendir");
    unsigned n = 0;
    while (auto* ent = readdir(dir)) {
        if (ent->d_name[0] != '.') ++n;
    }
    closedir(dir);
    return n;
}

static int pipeWithByte(unsigned char v) {
    int fds[2];
    require(pipe(fds) == 0, "pipe");
    require(write(fds[1], &v, 1) == 1, "write pipe");
    close(fds[1]);
    return fds[0];
}

struct TestEntry {
    using ID = pid_t;
    using NSID = pid_t;
    pid_t _id;
    pid_t _nsid;
    DarlingServer::EternalID _eid = 0;
    TestEntry(pid_t id, pid_t nsid) : _id(id), _nsid(nsid) {}
    pid_t id() const { return _id; }
    pid_t nsid() const { return _nsid; }
    DarlingServer::EternalID eternalID() const { return _eid; }
    void _setEternalID(DarlingServer::EternalID eid) { _eid = eid; }
};

static void testMessageDataBufferBasics() {
    MessageDataBuffer buf;
    require(buf.empty() && buf.size() == 0, "default buffer non-empty");
    require(buf.capacity() == 256, "inline capacity mismatch");
    require(reinterpret_cast<uintptr_t>(buf.data()) % 16 == 0, "inline buffer 16-byte alignment");

#ifdef USE_WRAP_MALLOC
    g_allocCount = 0;
    g_countAllocs = true;
#endif
    buf.prepareReceive(256);
    require(buf.size() == 256 && !buf.empty(), "prepareReceive size");
    for (size_t i = 0; i < 64; ++i) buf[i] = static_cast<uint8_t>(i + 1);
    buf.resize(64);
    require(buf.size() == 64, "shrink size");
    buf.resize(128);
    for (size_t i = 0; i < 64; ++i) require(buf[i] == static_cast<uint8_t>(i + 1), "inline prefix preserved");
    for (size_t i = 64; i < 128; ++i) require(buf[i] == 0, "grown inline bytes zero-filled");

    MessageDataBuffer movedCtor(std::move(buf));
    require(movedCtor.size() == 128 && buf.size() == 0, "inline move ctor sizes");
    for (size_t i = 0; i < 64; ++i) require(movedCtor[i] == static_cast<uint8_t>(i + 1), "inline move ctor data");

    MessageDataBuffer movedAssign;
    movedAssign.resize(32);
    movedAssign = std::move(movedCtor);
    require(movedAssign.size() == 128 && movedCtor.size() == 0, "inline move assign sizes");

    // Self-move safety (suppress -Wself-move via volatile pointer/reference)
    MessageDataBuffer* volatile selfPtr = &movedAssign;
    movedAssign = std::move(*selfPtr);
    require(movedAssign.size() == 128, "self-move inline size");
    const MessageDataBuffer& cbuf = movedAssign;
    require(cbuf[0] == 1 && cbuf.data()[63] == 64, "const accessors after self-move");
#ifdef USE_WRAP_MALLOC
    g_countAllocs = false;
    require(g_allocCount == 0, "inline MessageDataBuffer operations allocated on heap");
#endif

    // Spillover > 256 bytes
    movedAssign.resize(400);
    require(movedAssign.size() == 400 && movedAssign.capacity() >= 400, "heap spill resize");
    for (size_t i = 0; i < 64; ++i) require(movedAssign[i] == static_cast<uint8_t>(i + 1), "heap spill preserved prefix");
    for (size_t i = 64; i < 400; ++i) require(movedAssign[i] == 0, "heap spill zero-filled tail");

    // Shrink and re-grow within heap capacity
    movedAssign.resize(100);
    movedAssign.resize(300);
    for (size_t i = 100; i < 300; ++i) require(movedAssign[i] == 0, "regrown heap tail zero-filled");

    // Heap move ctor, heap->heap move assign, self-move on heap, inline->heap move assign
    MessageDataBuffer heapMoved(std::move(movedAssign));
    require(heapMoved.size() == 300 && movedAssign.size() == 0, "heap move ctor");
    MessageDataBuffer* volatile heapSelf = &heapMoved;
    heapMoved = std::move(*heapSelf);
    require(heapMoved.size() == 300 && heapMoved[0] == 1, "heap self-move");

    MessageDataBuffer anotherHeap;
    anotherHeap.resize(512);
    anotherHeap[0] = 99;
    anotherHeap = std::move(heapMoved);
    require(anotherHeap.size() == 300 && anotherHeap[0] == 1, "heap<-heap move assign");

    MessageDataBuffer inlineTarget;
    inlineTarget.resize(10);
    anotherHeap = std::move(inlineTarget);
    require(anotherHeap.size() == 10, "heap<-inline move assign");

    // prepareReceive spillover > 256
    MessageDataBuffer rxLarge;
    rxLarge.prepareReceive(512);
    require(rxLarge.size() == 512 && rxLarge.capacity() >= 512, "prepareReceive spillover");
}

static void testSocketpairRoundTrip() {
    int sv[2];
    require(socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, sv) == 0, "socketpair");

#ifdef USE_WRAP_MALLOC
    g_allocCount = 0;
    g_countAllocs = true;
#endif
    for (int iter = 0; iter < 100; ++iter) {
        MessageDataBuffer tx;
        tx.resize(48);
        for (size_t i = 0; i < 48; ++i) tx[i] = static_cast<uint8_t>((iter + i) & 0xFF);
        struct iovec txIov{ tx.data(), tx.size() };
        struct msghdr txHdr{};
        txHdr.msg_iov = &txIov;
        txHdr.msg_iovlen = 1;
        require(sendmsg(sv[0], &txHdr, 0) == 48, "sendmsg inline");

        MessageDataBuffer rxSlot;
        rxSlot.prepareReceive(256);
        struct iovec rxIov{ rxSlot.data(), rxSlot.size() };
        struct msghdr rxHdr{};
        rxHdr.msg_iov = &rxIov;
        rxHdr.msg_iovlen = 1;
        ssize_t n = recvmsg(sv[1], &rxHdr, 0);
        require(n == 48, "recvmsg inline");
        MessageDataBuffer rx = std::move(rxSlot);
        rx.resize(static_cast<size_t>(n));
        for (size_t i = 0; i < 48; ++i) require(rx[i] == static_cast<uint8_t>((iter + i) & 0xFF), "socketpair payload");
    }
#ifdef USE_WRAP_MALLOC
    g_countAllocs = false;
    require(g_allocCount == 0, "socketpair inline round-trip allocated on heap");
#endif

    // Spillover socketpair (> 256 bytes)
    MessageDataBuffer txBig;
    txBig.resize(400);
    for (size_t i = 0; i < 400; ++i) txBig[i] = static_cast<uint8_t>((i * 7) & 0xFF);
    struct iovec txIov{ txBig.data(), txBig.size() };
    struct msghdr txHdr{};
    txHdr.msg_iov = &txIov;
    txHdr.msg_iovlen = 1;
    require(sendmsg(sv[0], &txHdr, 0) == 400, "sendmsg spillover");

    MessageDataBuffer rxBig;
    rxBig.prepareReceive(512);
    struct iovec rxIov{ rxBig.data(), rxBig.size() };
    struct msghdr rxHdr{};
    rxHdr.msg_iov = &rxIov;
    rxHdr.msg_iovlen = 1;
    ssize_t n = recvmsg(sv[1], &rxHdr, 0);
    require(n == 400, "recvmsg spillover");
    rxBig.resize(static_cast<size_t>(n));
    for (size_t i = 0; i < 400; ++i) require(rxBig[i] == static_cast<uint8_t>((i * 7) & 0xFF), "spillover socketpair payload");

    close(sv[0]);
    close(sv[1]);
}

static void testRegistryFastPathAndConcurrency() {
    Registry<TestEntry> reg;

    // 1. Reentrant lookup inside factory must not deadlock; nullptr & throwing factories must clear guard.
    auto none = reg.registerIfAbsent(10, [&]() -> std::shared_ptr<TestEntry> {
        require(!reg.lookupEntryByID(1000).has_value(), "reentrant lookup during factory");
        return nullptr;
    });
    require(!none, "null factory returned entry");
    require(!reg.lookupEntryByNSID(10).has_value(), "null entry inserted");

    bool threw = false;
    try {
        reg.registerIfAbsent(11, [&]() -> std::shared_ptr<TestEntry> {
            throw std::runtime_error("boom");
        });
    } catch (const std::runtime_error&) {
        threw = true;
    }
    require(threw, "factory exception swallowed");
    // Verify _registeringWithLockHeld was reset by RAII guard after exception (lookup still works safely).
    require(!reg.lookupEntryByNSID(11).has_value(), "lookup after throwing factory");

    // 2. Concurrent registerIfAbsent (hot hits + contended cold misses) & lookupEntryByNSID under TSan/ASan
    std::atomic<int> factoryRuns[64]{};
    auto e0 = reg.registerIfAbsent(1000, []() { return std::make_shared<TestEntry>(1000, 1000); });
    require(e0 != nullptr && e0->eternalID() == 0, "first insert eternalID");

    constexpr int kThreads = 8;
    std::vector<std::thread> workers;
    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&, t]() {
            for (int i = 0; i < 500; ++i) {
                auto hot = reg.registerIfAbsent(1000, []() -> std::shared_ptr<TestEntry> {
                    throw std::runtime_error("hot hit must not call factory");
                });
                require(hot == e0, "hot hit mismatch");
                pid_t coldId = 2000 + (i % 64);
                auto cold = reg.registerIfAbsent(coldId, [&]() {
                    factoryRuns[i % 64].fetch_add(1, std::memory_order_relaxed);
                    return std::make_shared<TestEntry>(coldId + 10000, coldId);
                });
                require(cold && cold->nsid() == coldId, "cold insert mismatch");
                auto lu = reg.lookupEntryByNSID(coldId);
                require(lu.has_value() && *lu == cold, "concurrent lookup mismatch");
                (void)t;
            }
        });
    }
    for (auto& w : workers) w.join();
    for (int k = 0; k < 64; ++k) {
        require(factoryRuns[k].load() == 1, "factory invoked more than once for same key");
    }
}

static void testThreadSetAddress() {
    Thread thr;
    sockaddr_un addrA{};
    addrA.sun_family = AF_UNIX;
    std::strcpy(addrA.sun_path, "/tmp/test-addr-a");
    Address a(addrA, offsetof(sockaddr_un, sun_path) + std::strlen(addrA.sun_path) + 1);

    sockaddr_un addrB{};
    addrB.sun_family = AF_UNIX;
    std::strcpy(addrB.sun_path, "/tmp/test-addr-b");
    Address b(addrB, offsetof(sockaddr_un, sun_path) + std::strlen(addrB.sun_path) + 1);

    Address empty(addrA, 0);
    Address oversized(addrA, sizeof(sockaddr_un) + 8);

    thr.setAddress(empty);
    thr.setAddress(empty); // unchanged size==0 fast path
    require(thr.address().rawSize() == 0, "zero size address");

    thr.setAddress(a);
    thr.setAddress(a); // unchanged fast path
    require(thr.address().rawSize() == a.rawSize(), "address A size");
    require(std::memcmp(&thr.address().raw(), &a.raw(), a.rawSize()) == 0, "address A bytes");

    thr.setAddress(oversized);
    require(thr.address().rawSize() == sizeof(sockaddr_un) + 8, "oversized size stored safely");

    thr.setAddress(a);

    // Concurrent unchanged setAddress (shared_lock) + address() readers under TSan
    std::atomic<bool> stop{false};
    std::vector<std::thread> threads;
    for (int i = 0; i < 4; ++i) {
        threads.emplace_back([&]() {
            while (!stop.load(std::memory_order_relaxed)) {
                thr.setAddress(a);
                auto cur = thr.address();
                require(cur.rawSize() == a.rawSize(), "concurrent address A size");
            }
        });
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    stop.store(true, std::memory_order_relaxed);
    for (auto& th : threads) th.join();

    // Writer alternating unchanged and changed addresses while readers observe valid sizes
    stop.store(false);
    threads.clear();
    for (int i = 0; i < 3; ++i) {
        threads.emplace_back([&]() {
            while (!stop.load(std::memory_order_relaxed)) {
                auto cur = thr.address();
                require(cur.rawSize() == a.rawSize() || cur.rawSize() == b.rawSize(), "concurrent reader saw invalid size");
            }
        });
    }
    for (int i = 0; i < 2000; ++i) {
        thr.setAddress((i % 20 == 0) ? b : a);
    }
    stop.store(true, std::memory_order_relaxed);
    for (auto& th : threads) th.join();
}

static void testMessageControlAndDescriptorEdgeCases() {
    unsigned baseFDs = openFDs();

    // 1. Unused forReceive (inline & heap) destruction closes 0 FDs; zero-alloc inline send/recv
    {
#ifdef USE_WRAP_MALLOC
        g_allocCount = 0;
        g_countAllocs = true;
#endif
        Message rxInline = Message::forReceive(256, 4);
        Message rxMoved = std::move(rxInline);
        Message tx(64, 4);
        Message txMoved;
        txMoved = std::move(tx);
        Message* volatile selfPtr = &txMoved;
        txMoved = std::move(*selfPtr);
#ifdef USE_WRAP_MALLOC
        g_countAllocs = false;
        require(g_allocCount == 0, "inline Message construction/move allocated on heap");
#endif
        require(rxMoved.descriptors().empty(), "unused inline forReceive had descriptors");
        require(rxMoved.pid() == -1, "unused inline forReceive had credentials");

        Message rxHeap = Message::forReceive(512, 8);
        require(rxHeap.descriptors().empty() && rxHeap.pid() == -1, "unused heap forReceive");
    }
    require(openFDs() == baseFDs, "unused forReceive FD leak");

    // 2. Regression for _ensureDescriptorHeader when credHeader == nullptr and FDs fit in inlineControlCapacity (5..12 FDs)
    {
        int sv[2];
        require(socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, sv) == 0, "socketpair no passcred");
        int zero = 0;
        require(setsockopt(sv[1], SOL_SOCKET, SO_PASSCRED, &zero, sizeof(zero)) == 0, "SO_PASSCRED=0");

        // Send 6 FDs, receive via forReceive(64, 6) without SO_PASSCRED
        Message tx(64, 6);
        for (int i = 0; i < 6; ++i) tx.pushDescriptor(pipeWithByte(static_cast<unsigned char>(i)));
        tx.rawHeader().msg_name = nullptr;
        tx.rawHeader().msg_namelen = 0;
        require(sendmsg(sv[0], &tx.rawHeader(), 0) == 64, "sendmsg 6 fds");

        Message rx = Message::forReceive(64, 6);
        ssize_t n = recvmsg(sv[1], &rx.rawHeader(), MSG_CMSG_CLOEXEC);
        require(n == 64, "recvmsg 6 fds");
        require(rx.descriptors().size() == 6 && rx.pid() == -1, "received 6 fds without creds");

        // Grow via pushDescriptor across 7..12 (still <= 64B inlineControlCapacity!) and spill to 13..15 (heap)
        for (int i = 6; i < 15; ++i) {
            rx.pushDescriptor(pipeWithByte(static_cast<unsigned char>(i)));
        }
        require(rx.descriptors().size() == 15, "15 descriptors after pushDescriptor growth");
        for (int i = 0; i < 15; ++i) {
            int fd = rx.extractDescriptorAtIndex(0);
            unsigned char b = 0;
            require(read(fd, &b, 1) == 1 && b == static_cast<unsigned char>(i), "fd payload 0..14");
            close(fd);
        }

        // Exercise replaceDescriptors across 5..12 (inline without creds), 14 (heap), and shrink back to 2 & 0
        for (size_t count : {size_t(5), size_t(8), size_t(12), size_t(14), size_t(6), size_t(2), size_t(0)}) {
            for (int oldFd : rx.descriptors()) close(oldFd);
            std::vector<int> nextFDs;
            for (size_t j = 0; j < count; ++j) nextFDs.push_back(pipeWithByte(static_cast<unsigned char>(j)));
            rx.replaceDescriptors(nextFDs);
            require(rx.descriptors().size() == count, "replaceDescriptors count mismatch");
        }

        // Add 6 FDs without creds, then add credentials (spilling to heap), then shrink back to 2 FDs (heap -> inline)
        std::vector<int> six;
        for (int i = 0; i < 6; ++i) six.push_back(pipeWithByte(static_cast<unsigned char>(80 + i)));
        rx.replaceDescriptors(six);
        rx.setPID(4321);
        require(rx.pid() == 4321 && rx.descriptors().size() == 6, "creds + 6 fds");

        auto cur = rx.descriptors();
        std::vector<int> two = {cur[0], cur[1]};
        for (size_t i = 2; i < cur.size(); ++i) close(cur[i]);
        rx.replaceDescriptors(two);
        require(rx.pid() == 4321 && rx.descriptors().size() == 2, "shrunk heap -> inline with creds");

        close(sv[0]);
        close(sv[1]);
    }
    require(openFDs() == baseFDs, "descriptor edge-case FD leak");

    // 3. Inline zero-alloc AF_UNIX socketpair round-trip with SO_PASSCRED + SCM_RIGHTS
    {
        int sv[2];
        require(socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, sv) == 0, "socketpair passcred");
        int one = 1;
        require(setsockopt(sv[1], SOL_SOCKET, SO_PASSCRED, &one, sizeof(one)) == 0, "SO_PASSCRED=1");

        int p0 = pipeWithByte(33);
        int p1 = pipeWithByte(44);
#ifdef USE_WRAP_MALLOC
        g_allocCount = 0;
        g_countAllocs = true;
#endif
        Message tx(48, 2);
        tx.data()[0] = 0xAB;
        tx.pushDescriptor(p0);
        tx.pushDescriptor(p1);
        tx.rawHeader().msg_name = nullptr;
        tx.rawHeader().msg_namelen = 0;
        require(sendmsg(sv[0], &tx.rawHeader(), 0) == 48, "sendmsg inline with fds");

        Message rxSlot = Message::forReceive(256, 4);
        ssize_t n = recvmsg(sv[1], &rxSlot.rawHeader(), MSG_CMSG_CLOEXEC);
        require(n == 48, "recvmsg inline with fds");
        rxSlot.data().resize(static_cast<size_t>(n));
        Message rx = std::move(rxSlot);
#ifdef USE_WRAP_MALLOC
        g_countAllocs = false;
        require(g_allocCount == 0, "inline Message socketpair cycle allocated on heap");
#endif
        require(rx.data().size() == 48 && rx.data()[0] == 0xAB, "inline Message payload");
        require(rx.pid() == getpid() && rx.descriptors().size() == 2, "inline Message creds+fds");

        for (unsigned char expected : {33, 44}) {
            int fd = rx.extractDescriptorAtIndex(0);
            unsigned char b = 0;
            require(read(fd, &b, 1) == 1 && b == expected, "inline rx fd payload");
            close(fd);
        }
        close(sv[0]);
        close(sv[1]);
    }
    require(openFDs() == baseFDs, "inline socketpair FD leak");
}

int main() {
    try {
        Message::initializeServerCredentials();
        testMessageDataBufferBasics();
        testMessageControlAndDescriptorEdgeCases();
        testSocketpairRoundTrip();
        testRegistryFastPathAndConcurrency();
        testThreadSetAddress();
        std::printf("RPC_FASTPATH_TEST pass=1\n");
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "RPC_FASTPATH_TEST pass=0 error=%s\n", e.what());
        return 1;
    }
}
'''


def _compiler_supports_flags(compiler, flags, tmpdir):
    probe = Path(tmpdir) / 'san_probe.cpp'
    if not probe.exists():
        probe.write_text('#include <thread>\nint main() { std::thread t([]{}); t.join(); return 0; }\n')
    exe = Path(tmpdir) / ('probe_' + '_'.join(f.strip('-') for f in flags).replace(',', '_').replace('=', '_'))
    build = subprocess.run([compiler, '-std=c++17', *flags, str(probe), '-pthread', '-o', str(exe)],
                           capture_output=True, text=True)
    if build.returncode != 0:
        return False
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=15)
    return run.returncode == 0


class RpcFastpathSourceTests(unittest.TestCase):
    def setUp(self):
        self.message_hpp = patched('internal-include/darlingserver/message.hpp')
        self.message_cpp = patched('src/message.cpp')
        self.server_cpp = patched('src/server.cpp')
        self.registry_hpp = patched('internal-include/darlingserver/registry.hpp')
        self.thread_hpp = patched('internal-include/darlingserver/thread.hpp')
        self.thread_cpp = patched('src/thread.cpp')

    def test_message_inline_data_and_control_invariants(self):
        self.assertIn('static constexpr size_t inlineCapacity = 256;', self.message_hpp)
        self.assertIn('alignas(16) uint8_t _inline[inlineCapacity];', self.message_hpp)
        self.assertIn('static constexpr size_t inlineControlDescriptors = 4;', self.message_hpp)
        self.assertIn('CMSG_SPACE(sizeof(struct ucred)) + CMSG_SPACE(sizeof(int) * inlineControlDescriptors)',
                      self.message_hpp)
        self.assertIn('alignas(struct cmsghdr) uint8_t _inlineControl[inlineControlCapacity];', self.message_hpp)
        self.assertIn('bool _usingInlineControl() const noexcept', self.message_hpp)
        self.assertIn('static Message forReceive(size_t bufferSpace = 256, size_t descriptorSpace = 4);',
                      self.message_hpp)
        self.assertIn('MessageDataBuffer& data();', self.message_hpp)
        self.assertIn('const Address& address() const;', self.message_hpp)
        self.assertIn('void setAddress(const Address& address);', self.message_hpp)

    def test_message_for_receive_and_cleanup_guards(self):
        for_recv = extract_brace_block(
            self.message_cpp,
            r'^DarlingServer::Message::Message\(ReceiveTag, size_t bufferSpace, size_t descriptorSpace\)\s*\{',
        )
        self.assertIn('_buffer.prepareReceive(bufferSpace);', for_recv)
        self.assertIn('memset(_controlHeader, 0, sizeof(struct cmsghdr));', for_recv)
        self.assertNotIn('outgoingCredentials()', for_recv)
        self.assertNotIn('SCM_RIGHTS', for_recv)

        cleanup = patched_hunk('src/message.cpp', 'DarlingServer::Message::_cleanupSelf()')
        self.assertIn('if (!_usingInlineControl())', cleanup)

        ensure_cred = patched_hunk('src/message.cpp', 'DarlingServer::Message::_ensureCredentialsHeader()')
        self.assertIn('if (!oldInline)', ensure_cred)

        ensure_desc = patched_hunk('src/message.cpp', 'DarlingServer::Message::_ensureDescriptorHeader(size_t newSpace)')
        self.assertIn('!_usingInlineControl()', ensure_desc)
        self.assertIn('!oldInline', ensure_desc)

        init_other = self.message_cpp[self.message_cpp.index('void DarlingServer::Message::_initWithOther(Message&& other) {'):]
        init_other = init_other[:init_other.index('_cleanupSelf')]
        self.assertIn('other._usingInlineControl()', init_other)
        self.assertIn('_header.msg_control = _controlHeader;', init_other)
        self.assertIn('_header.msg_name = &_socketAddress.raw();', init_other)
        self.assertIn('_header.msg_iov = &_dataDescriptor;', init_other)
        raw_hdr = patched_hunk('src/message.cpp', 'Message(Message&& other)')
        self.assertIn('_dataDescriptor.iov_base = _buffer.data();', raw_hdr)

        op_assign = extract_brace_block(
            self.message_cpp, r'^DarlingServer::Message& DarlingServer::Message::operator=\(Message&& other\)\s*\{'
        )
        self.assertIn('if (this == &other)', op_assign)

        cred_hdr = patched_hunk('src/message.cpp', 'DarlingServer::Message::_credentialsHeader() const')
        desc_hdr = patched_hunk('src/message.cpp', 'DarlingServer::Message::_descriptorHeader() const')
        self.assertIn('if (hdr->cmsg_len < sizeof(struct cmsghdr))', cred_hdr)
        self.assertIn('if (hdr->cmsg_len < sizeof(struct cmsghdr))', desc_hdr)

    def test_receive_call_sites_use_for_receive(self):
        arm = extract_brace_block(self.server_cpp, r'^void DarlingServer::Server::_ringArmReceive\(size_t index\)\s*\{')
        self.assertIn('slot.emplace(Message::forReceive());', arm)

        complete = extract_brace_block(
            self.server_cpp, r'^void DarlingServer::Server::_ringComplete\(const Ring::Completion& completion\)\s*\{'
        )
        self.assertLess(complete.index('slot->data().resize(completion.result);'),
                        complete.index('Message message = std::move(*slot);'))

        recv_many = patched_hunk('src/message.cpp', 'DarlingServer::MessageQueue::receiveMany(int socket)')
        self.assertIn('_receiveMessages[i].emplace(Message::forReceive());', recv_many)

    def test_registry_and_thread_address_fast_paths(self):
        self.assertIn('template<class Factory>', self.registry_hpp)
        self.assertNotIn('std::function<std::shared_ptr<Entry>()>', self.registry_hpp)
        reg_fn = extract_brace_block(
            self.registry_hpp,
            r'^\t\tstd::shared_ptr<Entry> registerIfAbsent\(NSID nsid, Factory&& entryFactory\)\s*\{',
        )
        shared_idx = reg_fn.index('std::shared_lock lock(_rwlock);')
        unique_idx = reg_fn.index('std::unique_lock lock(_rwlock);')
        self.assertLess(shared_idx, unique_idx)
        self.assertEqual(reg_fn.count('_nsmap.find(nsid)'), 2)
        self.assertLess(unique_idx, reg_fn.index('_registeringWithLockHeld'))

        self.assertIn('void setAddress(const Address& address);', self.thread_hpp)
        set_addr = extract_thread_set_address()
        self.assertLess(set_addr.index('std::shared_lock lock(_rwlock);'),
                        set_addr.index('std::unique_lock lock(_rwlock);'))
        self.assertIn('address.rawSize() <= sizeof(struct sockaddr_un)', set_addr)
        self.assertIn('std::memcmp(&_address.raw(), &address.raw(), _address.rawSize()) == 0', set_addr)


@unittest.skipUnless(shutil.which('c++'), 'requires C++17 compiler')
class RpcFastpathExtractedRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which('c++')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        source_text = (
            HARNESS_CPP
            .replace('__MESSAGE_DATA_BUFFER__', extract_message_data_buffer())
            .replace('__MESSAGE_PRIVATE_FIELDS__', extract_message_private_header_block())
            .replace('__REGISTRY_REGISTER_IF_ABSENT__', extract_registry_register_if_absent())
            .replace('__THREAD_SET_ADDRESS__', extract_thread_set_address())
            .replace('__MESSAGE_METHODS__', extract_message_methods_from_patch())
        )
        cls.source = Path(cls.tmp.name) / 'rpc_fastpath_test.cpp'
        cls.source.write_text(source_text)

    def test_zero_alloc_and_socketpair_with_wrap_malloc(self):
        binary = Path(self.tmp.name) / 'rpc_wrap'
        subprocess.run([
            self.compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
            '-DUSE_WRAP_MALLOC', '-pthread', str(self.source), '-Wl,--wrap=malloc',
            '-o', str(binary),
        ], check=True, capture_output=True, text=True)
        res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('RPC_FASTPATH_TEST pass=1', res.stdout)

    def test_asan_ubsan(self):
        flags = ['-O1', '-g', '-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        if not _compiler_supports_flags(self.compiler, flags, self.tmp.name):
            self.skipTest('ASan/UBSan runtime not supported on this host')
        binary = Path(self.tmp.name) / 'rpc_asan'
        subprocess.run([
            self.compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
            *flags, '-pthread', str(self.source), '-o', str(binary),
        ], check=True, capture_output=True, text=True)
        res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('RPC_FASTPATH_TEST pass=1', res.stdout)

    def test_tsan(self):
        flags = ['-O1', '-g', '-fsanitize=thread', '-fno-omit-frame-pointer']
        if not _compiler_supports_flags(self.compiler, flags, self.tmp.name):
            self.skipTest('TSan runtime not supported on this host')
        binary = Path(self.tmp.name) / 'rpc_tsan'
        subprocess.run([
            self.compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
            *flags, '-pthread', str(self.source), '-o', str(binary),
        ], check=True, capture_output=True, text=True)
        res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('RPC_FASTPATH_TEST pass=1', res.stdout)


if __name__ == '__main__':
    unittest.main()
