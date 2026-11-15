/* Runs darlingserver's mask-free context switch through the patterns
 * src/thread.cpp uses it for. The assembly is linked in from the patch by
 * tests/test_darlingserver_context_switch.py. Prints "OK" on success. */
#define _GNU_SOURCE
#include <fenv.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <ucontext.h>
#include <xmmintrin.h>

int dserver_getcontext(ucontext_t* context) __attribute__((returns_twice));
int dserver_setcontext(const ucontext_t* context);

#define STACK_SIZE (64 * 1024)
#define CHECK(condition) do { if (!(condition)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); exit(1); } } while (0)

static __thread ucontext_t top;
static __thread volatile int returningToTop;

/* --- a call that runs to completion on a fresh stack (doWork -> microthreadWorker) --- */

static volatile uint64_t workerRan;
static volatile void* workerStackSeen;

static void worker(void) {
	int marker;
	workerStackSeen = &marker;
	++workerRan;
	dserver_setcontext(&top);
	abort();
}

__attribute__((noinline)) static uint64_t run_worker(char* stack, uint64_t a, uint64_t b, uint64_t c, uint64_t d, uint64_t e) {
	/* enough live values that some sit in callee-saved registers across the switch */
	volatile uint64_t keep = a * 3 + b;
	returningToTop = 0;
	dserver_getcontext(&top);
	if (!returningToTop) {
		returningToTop = 1;
		ucontext_t fresh;
		dserver_getcontext(&fresh);
		fresh.uc_stack.ss_sp = stack;
		fresh.uc_stack.ss_size = STACK_SIZE;
		fresh.uc_stack.ss_flags = 0;
		fresh.uc_link = &top;
		makecontext(&fresh, worker, 0);
		dserver_setcontext(&fresh);
		abort();
	}
	return keep + a + b * 5 + c * 7 + d * 11 + e * 13;
}

/* --- a call that suspends and is resumed later (Thread::suspend / doWork resume) --- */

static ucontext_t resumeContext;
static volatile int suspended;
static volatile int steps;
static volatile double suspendedSum;

static void suspend_here(void) {
	suspended = 1;
	dserver_getcontext(&resumeContext);
	if (suspended) {
		dserver_setcontext(&top);
		abort();
	}
}

static void suspending_worker(void) {
	double sum = 0.5;
	for (int i = 0; i < 3; ++i) {
		++steps;
		sum = sum * 2.0 + i;
		suspend_here();
	}
	suspendedSum = sum;
	++steps;
	dserver_setcontext(&top);
	abort();
}

static void start_suspending(char* stack) {
	returningToTop = 0;
	dserver_getcontext(&top);
	if (!returningToTop) {
		returningToTop = 1;
		ucontext_t fresh;
		dserver_getcontext(&fresh);
		fresh.uc_stack.ss_sp = stack;
		fresh.uc_stack.ss_size = STACK_SIZE;
		fresh.uc_stack.ss_flags = 0;
		fresh.uc_link = &top;
		makecontext(&fresh, suspending_worker, 0);
		dserver_setcontext(&fresh);
		abort();
	}
}

static void resume_suspended(void) {
	returningToTop = 0;
	dserver_getcontext(&top);
	if (!returningToTop) {
		returningToTop = 1;
		suspended = 0;
		dserver_setcontext(&resumeContext);
		abort();
	}
}

static void* resume_on_other_thread(void* unused) {
	(void)unused;
	resume_suspended();
	return NULL;
}

/* --- floating-point control state belongs to the context --- */

static void rounding_worker(void) {
	fesetround(FE_UPWARD);
	_MM_SET_ROUNDING_MODE(_MM_ROUND_UP);
	dserver_setcontext(&top);
	abort();
}

static void run_rounding_worker(char* stack) {
	returningToTop = 0;
	dserver_getcontext(&top);
	if (!returningToTop) {
		returningToTop = 1;
		ucontext_t fresh;
		dserver_getcontext(&fresh);
		fresh.uc_stack.ss_sp = stack;
		fresh.uc_stack.ss_size = STACK_SIZE;
		fresh.uc_stack.ss_flags = 0;
		fresh.uc_link = &top;
		makecontext(&fresh, rounding_worker, 0);
		dserver_setcontext(&fresh);
		abort();
	}
}

static double now_ns(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e9 + ts.tv_nsec;
}

int main(int argc, char** argv) {
	char* stack = malloc(STACK_SIZE);
	char* stack2 = malloc(STACK_SIZE);
	CHECK(stack && stack2);

	/* fresh stack, run, back to the top; locals and callee-saved registers survive */
	uint64_t expected = (1 * 3 + 2) + 1 + 2 * 5 + 3 * 7 + 4 * 11 + 5 * 13;
	CHECK(run_worker(stack, 1, 2, 3, 4, 5) == expected);
	CHECK(workerRan == 1);
	CHECK((char*)workerStackSeen >= stack && (char*)workerStackSeen < stack + STACK_SIZE);

	/* suspend three times, resume on this thread, then on another one */
	start_suspending(stack2);
	CHECK(steps == 1 && suspended);
	resume_suspended();
	CHECK(steps == 2 && suspended);
	pthread_t other;
	CHECK(pthread_create(&other, NULL, resume_on_other_thread, NULL) == 0);
	CHECK(pthread_join(other, NULL) == 0);
	CHECK(steps == 3 && suspended);
	resume_suspended();
	CHECK(steps == 4);
	CHECK(suspendedSum == ((0.5 * 2.0 + 0) * 2.0 + 1) * 2.0 + 2);

	/* a rounding mode set inside a microthread does not leak into the top context */
	CHECK(fegetround() == FE_TONEAREST);
	run_rounding_worker(stack);
	CHECK(fegetround() == FE_TONEAREST);
	CHECK(_MM_GET_ROUNDING_MODE() == _MM_ROUND_NEAREST);

	/* the switch leaves the signal mask alone: that is the syscall it no longer makes */
	sigset_t blocked, seen;
	sigemptyset(&blocked);
	sigaddset(&blocked, SIGUSR2);
	/* the mask survives exec, so start from a known state */
	CHECK(pthread_sigmask(SIG_UNBLOCK, &blocked, NULL) == 0);
	ucontext_t before;
	volatile int resumed = 0;
	dserver_getcontext(&before);
	if (!resumed) {
		resumed = 1;
		CHECK(pthread_sigmask(SIG_BLOCK, &blocked, NULL) == 0);
		dserver_setcontext(&before);
		abort();
	}
	CHECK(pthread_sigmask(SIG_BLOCK, NULL, &seen) == 0);
	CHECK(sigismember(&seen, SIGUSR2) == 1);
	CHECK(pthread_sigmask(SIG_UNBLOCK, &blocked, NULL) == 0);

	/* many calls in a row, as the server does */
	int rounds = argc > 1 ? atoi(argv[1]) : 200000;
	uint64_t ranBefore = workerRan;
	double start = now_ns();
	for (int i = 0; i < rounds; ++i) {
		CHECK(run_worker(stack, i, 2, 3, 4, 5) == (uint64_t)i * 3 + 2 + i + 2 * 5 + 3 * 7 + 4 * 11 + 5 * 13);
	}
	double perCall = (now_ns() - start) / rounds;
	CHECK(workerRan == ranBefore + (uint64_t)rounds);

	printf("OK %.0f ns per call\n", perCall);
	free(stack);
	free(stack2);
	return 0;
}
