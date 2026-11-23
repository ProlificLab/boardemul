/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * A sampling profiler without perf: run a command as our child, seize each
 * of its threads with ptrace, and every PERIOD microseconds stop each thread
 * for an instant and record where it is.
 *
 *   qemu_sampler OUT PERIOD_US -- CMD ARGS...
 *
 * OUT gets one line per sample, "tid rip" and then the words above the
 * thread's stack pointer that point into code, its likely callers; OUT.maps
 * the child's /proc/PID/maps, refreshed while it runs. Tracing our own child needs no
 * CAP_SYS_PTRACE, which a Docker container does not have (ADR 0176).
 * Built and used by tools/profile.sh.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_THREADS 512

static pid_t tids[MAX_THREADS];
static int ntids;
static int pending[MAX_THREADS];        /* interrupted, its stop not yet seen */
static pid_t child;
static volatile sig_atomic_t stop_flag;
static char maps_out[512];
static int verbose;

static int find(pid_t tid)
{
    for (int i = 0; i < ntids; i++)
        if (tids[i] == tid)
            return i;
    return -1;
}

static void forget(pid_t tid)
{
    int i = find(tid);
    if (i < 0)
        return;
    tids[i] = tids[ntids - 1];
    pending[i] = pending[ntids - 1];
    ntids--;
}

static void seize_new(void)
{
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/task", child);
    DIR *d = opendir(path);
    if (!d)
        return;
    struct dirent *e;
    while ((e = readdir(d))) {
        pid_t tid = atoi(e->d_name);
        if (tid <= 0 || find(tid) >= 0 || ntids >= MAX_THREADS)
            continue;
        if (ptrace(PTRACE_SEIZE, tid, 0, 0) == 0) {
            pending[ntids] = 0;
            tids[ntids++] = tid;
            if (verbose)
                fprintf(stderr, "sampler: seized %d\n", tid);
        } else if (verbose) {
            fprintf(stderr, "sampler: seize %d: %s\n", tid, strerror(errno));
        }
    }
    closedir(d);
}

static void load_code_ranges(const char *maps, size_t n);

static void save_maps(void)
{
    char path[64];
    static char buf[1 << 20];
    snprintf(path, sizeof path, "/proc/%d/maps", child);
    FILE *in = fopen(path, "r");
    if (!in)
        return;
    size_t n = fread(buf, 1, sizeof buf, in);
    fclose(in);
    if (n == 0)
        return;
    FILE *o = fopen(maps_out, "w");
    if (!o)
        return;
    fwrite(buf, 1, n, o);
    fclose(o);
    load_code_ranges(buf, n);
}

/* Executable file-backed ranges of the child, for scanning stacks. */
#define MAX_RANGES 256
static unsigned long code_lo[MAX_RANGES], code_hi[MAX_RANGES];
static int ncode;

static void load_code_ranges(const char *maps, size_t n)
{
    ncode = 0;
    const char *p = maps, *end = maps + n;
    while (p < end && ncode < MAX_RANGES) {
        const char *nl = memchr(p, '\n', end - p);
        if (!nl)
            nl = end;
        unsigned long lo, hi;
        char perms[8] = "";
        if (sscanf(p, "%lx-%lx %7s", &lo, &hi, perms) == 3 && perms[2] == 'x'
            && memchr(p, '/', nl - p)) {
            code_lo[ncode] = lo;
            code_hi[ncode++] = hi;
        }
        p = nl + 1;
    }
}

static int is_code(unsigned long a)
{
    for (int i = 0; i < ncode; i++)
        if (a >= code_lo[i] && a < code_hi[i])
            return 1;
    return 0;
}

/* Up to `max` words above the stack pointer that point into code: the
 * likely return addresses, nearest first. */
static void scan_stack(FILE *out, pid_t tid, unsigned long rsp, int max)
{
    unsigned long words[256];
    struct iovec local = { words, sizeof words };
    struct iovec remote = { (void *)rsp, sizeof words };
    ssize_t got = process_vm_readv(tid, &local, 1, &remote, 1, 0);
    int found = 0;
    for (ssize_t i = 0; i < got / (ssize_t)sizeof(long) && found < max; i++) {
        if (is_code(words[i])) {
            fprintf(out, " %lx", words[i]);
            found++;
        }
    }
}

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void on_term(int sig)
{
    (void)sig;
    stop_flag = 1;
}

int main(int argc, char **argv)
{
    if (argc < 5 || strcmp(argv[3], "--") != 0) {
        fprintf(stderr, "usage: qemu_sampler OUT PERIOD_US -- CMD ARGS...\n");
        return 2;
    }
    verbose = getenv("SAMPLER_VERBOSE") != NULL;
    FILE *out = fopen(argv[1], "w");
    if (!out) {
        perror(argv[1]);
        return 2;
    }
    double period = atof(argv[2]) / 1e6;
    snprintf(maps_out, sizeof maps_out, "%s.maps", argv[1]);
    child = fork();
    if (child == 0) {
        execvp(argv[4], argv + 4);
        perror(argv[4]);
        _exit(127);
    }
    /* What is sent to us is for the command: pass a stop request on. */
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    usleep(50000);                      /* past the exec */
    seize_new();
    double next = now() + period, rescan = now() + 0.05;
    long samples = 0, signals = 0;
    int forwarded = 0;
    for (;;) {
        if (stop_flag && !forwarded) {
            kill(child, SIGTERM);
            forwarded = 1;
        }
        int status;
        pid_t w = waitpid(-1, &status, __WALL | WNOHANG);
        if (w > 0) {
            if (WIFEXITED(status) || WIFSIGNALED(status)) {
                forget(w);
                if (w == child)
                    break;
                continue;
            }
            if (WIFSTOPPED(status)) {
                int i = find(w);
                int event = status >> 16;
                int sig = WSTOPSIG(status);
                if (event == PTRACE_EVENT_STOP) {
                    if (i >= 0 && pending[i]) {
                        struct user_regs_struct regs;
                        if (ptrace(PTRACE_GETREGS, w, 0, &regs) == 0) {
                            fprintf(out, "%d %llx", w, (unsigned long long)regs.rip);
                            scan_stack(out, w, regs.rsp, 8);
                            fputc('\n', out);
                            samples++;
                        }
                        pending[i] = 0;
                    }
                    if (sig == SIGSTOP || sig == SIGTSTP || sig == SIGTTIN || sig == SIGTTOU)
                        ptrace(PTRACE_LISTEN, w, 0, 0);     /* a real group stop */
                    else
                        ptrace(PTRACE_CONT, w, 0, 0);
                } else if (event == 0) {
                    signals++;
                    if (verbose)
                        fprintf(stderr, "sampler: %d signal %d\n", w, sig);
                    ptrace(PTRACE_CONT, w, 0, (void *)(long)sig);   /* deliver it */
                } else {
                    ptrace(PTRACE_CONT, w, 0, 0);
                }
            }
            continue;
        }
        double t = now();
        if (t >= rescan) {
            seize_new();
            save_maps();
            rescan = t + 0.2;
        }
        if (t >= next) {
            for (int i = 0; i < ntids; i++) {
                if (!pending[i] && ptrace(PTRACE_INTERRUPT, tids[i], 0, 0) == 0)
                    pending[i] = 1;
            }
            next += period;
            if (next < t)
                next = t + period;
        } else {
            usleep(20);
        }
    }
    fprintf(stderr, "sampler: %ld samples, %ld signals passed on, %d threads at the end\n",
            samples, signals, ntids);
    fclose(out);
    return 0;
}
