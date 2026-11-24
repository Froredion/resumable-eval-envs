/*
 * librandpin: pinned randomness for eval environments (LD_PRELOAD).
 *
 * Replaces the kernel's random source, as libc hands it out, with a
 * reproducible stream, so a rebuilt environment generates the same UUIDs,
 * temp names and database random values as the original run.
 *
 * Each process gets its own ChaCha20 stream, keyed by RESUMABLE_EVALS_SEED,
 * with a nonce of (the step's pinned time, a counter). The pinned time is
 * the content of FAKETIME_TIMESTAMP_FILE (the clock the orchestrator pins
 * every step). The counter is taken on the process's first draw by
 * atomically incrementing RESUMABLE_EVALS_RANDOM_DIR/<time> under flock,
 * so processes drawing in the same step get distinct streams, and the
 * same streams on rebuild as long as they draw in the same order. A
 * forked child (no exec: Postgres backends) takes a fresh counter on its
 * own first draw instead of repeating its parent's bytes. Draws before the
 * harness starts (the environment's boot, whose length varies) count
 * separately from the harness's; see booting().
 *
 * Covered: getrandom(), getentropy(), arc4random*(), syscall(SYS_getrandom),
 * reads of /dev/urandom and /dev/random opened through libc, and OpenSSL's
 * RAND_*bytes() when libcrypto is a shared library (Postgres
 * gen_random_uuid() and the seed of random()). OpenSSL's own generator
 * cannot be pinned from its seed alone: it mixes the pid, thread id and
 * time into its nonce.
 *
 * If the counter cannot be taken, the process falls back to real
 * randomness and a line is appended to RESUMABLE_EVALS_RANDOM_DIR/errors
 * (never stderr: that could change tool output). Without
 * RESUMABLE_EVALS_SEED the shim passes everything through.
 *
 * Not deterministic: the order in which threads of one process draw, and
 * processes that draw on timers (their counter order can change). Both
 * show up as divergences in the per-step result check. Keys derived from
 * this stream are predictable; eval environments are offline.
 *
 * Build: cc -O2 -shared -fPIC -o librandpin.so randpin.c -ldl -lpthread
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

/* -- ChaCha20 ---------------------------------------------------------- */

#define ROTL(a, b) (((a) << (b)) | ((a) >> (32 - (b))))
#define QR(a, b, c, d) \
    (a += b, d ^= a, d = ROTL(d, 16), c += d, b ^= c, b = ROTL(b, 12), \
     a += b, d ^= a, d = ROTL(d, 8), c += d, b ^= c, b = ROTL(b, 7))

static void chacha_block(const uint32_t in[16], uint8_t out[64]) {
    uint32_t x[16];
    memcpy(x, in, sizeof x);
    for (int i = 0; i < 10; i++) {
        QR(x[0], x[4], x[8], x[12]); QR(x[1], x[5], x[9], x[13]);
        QR(x[2], x[6], x[10], x[14]); QR(x[3], x[7], x[11], x[15]);
        QR(x[0], x[5], x[10], x[15]); QR(x[1], x[6], x[11], x[12]);
        QR(x[2], x[7], x[8], x[13]); QR(x[3], x[4], x[9], x[14]);
    }
    for (int i = 0; i < 16; i++) {
        uint32_t v = x[i] + in[i];
        out[4 * i] = v; out[4 * i + 1] = v >> 8; out[4 * i + 2] = v >> 16; out[4 * i + 3] = v >> 24;
    }
}

/* -- per-process stream ------------------------------------------------ */

enum { UNINIT, PINNED, PASSTHROUGH };

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static int state = UNINIT;
static uint32_t ctx[16];
static uint8_t buf[64];
static size_t used = 64;
static pthread_once_t atfork_once = PTHREAD_ONCE_INIT;

static void child_after_fork(void) {
    pthread_mutex_init(&lock, NULL);
    state = UNINIT;  /* take a counter of our own on the next draw */
    used = 64;
}

static void register_atfork(void) { pthread_atfork(NULL, NULL, child_after_fork); }

static long raw_getrandom(void *p, size_t n, unsigned flags) {
    return syscall(SYS_getrandom, p, n, flags);
}

static const char *random_dir(void) {
    const char *d = getenv("RESUMABLE_EVALS_RANDOM_DIR");
    return d && *d ? d : "/run/resumable-evals/random";
}

static void report(const char *what) {
    int err = errno;
    char path[4096], line[512];
    snprintf(path, sizeof path, "%s/errors", random_dir());
    int fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0666);
    if (fd < 0) return;
    char exe[256] = "?";
    ssize_t k = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (k > 0) exe[k] = 0;
    int m = snprintf(line, sizeof line, "pid %d (%s): %s: %s\n", (int)getpid(), exe, what, strerror(err));
    if (m > 0) (void)!write(fd, line, (size_t)m < sizeof line ? (size_t)m : sizeof line - 1);
    close(fd);
}

/* The step's pinned time, as libfaketime reads it: "1764000000" or "@1764000000". */
static uint64_t pinned_time(void) {
    const char *f = getenv("FAKETIME_TIMESTAMP_FILE");
    if (!f) return 0;
    int fd = open(f, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char s[64] = {0};
    ssize_t n = read(fd, s, sizeof s - 1);
    close(fd);
    if (n <= 0) return 0;
    const char *p = s[0] == '@' ? s + 1 : s;
    return strtoull(p, NULL, 10);
}

/* Counter files are shared by every user's processes (root, postgres, the
 * agent's). The directory is world-writable but not sticky, and an existing
 * file is opened without O_CREAT: with fs.protected_regular, an O_CREAT open
 * of another user's file in a sticky directory is refused, even to root. */
static int open_counter(const char *path) {
    for (int i = 0; i < 1000; i++) {
        int fd = open(path, O_RDWR | O_CLOEXEC);
        if (fd >= 0) return fd;
        if (errno == ENOENT) {
            fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
            if (fd >= 0) { fchmod(fd, 0666); return fd; }  /* umask may have narrowed it */
            if (errno == ENOENT && (mkdir(random_dir(), 0777) == 0 || errno == EEXIST)) {
                chmod(random_dir(), 0777);
                continue;
            }
            if (errno == EEXIST) continue;  /* another process just created it */
            return -1;
        }
        if (errno != EACCES) return -1;
        usleep(1000);  /* just created by another user, not yet opened up */
    }
    return -1;
}

/* Until the orchestrator creates RESUMABLE_EVALS_RANDOM_DIR/harness-started,
 * the environment is booting: its services and readiness checks draw a
 * number of times that depends on how long boot takes, so they count apart
 * from the harness's processes, even when both share a pinned second. */
static int booting(void) {
    char path[4096];
    snprintf(path, sizeof path, "%s/harness-started", random_dir());
    return access(path, F_OK) != 0;
}

static int take_counter(uint64_t t, int boot, uint32_t *out) {
    char path[4096];
    snprintf(path, sizeof path, "%s/%s%llu", random_dir(), boot ? "boot-" : "", (unsigned long long)t);
    int fd = open_counter(path);
    if (fd < 0) { report("open counter"); return -1; }
    if (flock(fd, LOCK_EX) != 0) { report("flock counter"); close(fd); return -1; }
    char s[32] = {0};
    ssize_t n = pread(fd, s, sizeof s - 1, 0);
    uint32_t c = n > 0 ? (uint32_t)strtoul(s, NULL, 10) : 0;
    int m = snprintf(s, sizeof s, "%u\n", c + 1);
    if (ftruncate(fd, 0) != 0 || pwrite(fd, s, (size_t)m, 0) != m) { report("write counter"); close(fd); return -1; }
    close(fd);  /* releases the lock */
    *out = c;
    return 0;
}

/* Caller holds lock. */
static void init_stream(void) {
    pthread_once(&atfork_once, register_atfork);
    const char *seed_s = getenv("RESUMABLE_EVALS_SEED");
    if (!seed_s || !*seed_s) { state = PASSTHROUGH; return; }
    uint64_t seed = strtoull(seed_s, NULL, 10);
    uint64_t t = pinned_time();
    uint32_t counter;
    int boot = booting();
    if (take_counter(t, boot, &counter) != 0) { state = PASSTHROUGH; return; }
    static const uint32_t sigma[4] = {0x61707865, 0x3320646e, 0x79622d32, 0x6b206574};
    memcpy(ctx, sigma, sizeof sigma);
    /* key: seed, then a fixed tag so seed 0 is not an all-zero key */
    ctx[4] = (uint32_t)seed; ctx[5] = (uint32_t)(seed >> 32);
    ctx[6] = 0x72616e64; ctx[7] = 0x70696e31; ctx[8] = (uint32_t)boot; ctx[9] = ctx[10] = ctx[11] = 0;
    ctx[12] = 0;  /* block counter */
    ctx[13] = (uint32_t)t; ctx[14] = (uint32_t)(t >> 32); ctx[15] = counter;
    used = 64;
    state = PINNED;
}

/* Fills p with n bytes; returns 0, or -1 if this process passes through. */
static int pinned_bytes(void *p, size_t n) {
    pthread_mutex_lock(&lock);
    if (state == UNINIT) init_stream();
    if (state != PINNED) { pthread_mutex_unlock(&lock); return -1; }
    uint8_t *o = p;
    while (n) {
        if (used == 64) { chacha_block(ctx, buf); ctx[12]++; used = 0; }
        size_t k = 64 - used < n ? 64 - used : n;
        memcpy(o, buf + used, k);
        used += k; o += k; n -= k;
    }
    pthread_mutex_unlock(&lock);
    return 0;
}

static void fill(void *p, size_t n) {
    if (pinned_bytes(p, n) == 0) return;
    uint8_t *o = p;
    while (n) {
        long k = raw_getrandom(o, n, 0);
        if (k <= 0) { if (errno == EINTR) continue; return; }
        o += k; n -= (size_t)k;
    }
}

/* -- libc entry points ------------------------------------------------- */

ssize_t getrandom(void *p, size_t n, unsigned int flags) {
    if (pinned_bytes(p, n) == 0) return (ssize_t)n;
    return raw_getrandom(p, n, flags);
}

int getentropy(void *p, size_t n) {
    if (n > 256) { errno = EIO; return -1; }
    fill(p, n);
    return 0;
}

uint32_t arc4random(void) { uint32_t v; fill(&v, sizeof v); return v; }
void arc4random_buf(void *p, size_t n) { fill(p, n); }
uint32_t arc4random_uniform(uint32_t bound) {
    if (bound < 2) return 0;
    uint32_t min = -bound % bound, v;  /* rejection sampling, no modulo bias */
    do v = arc4random(); while (v < min);
    return v % bound;
}

long syscall(long number, ...) {
    va_list ap;
    va_start(ap, number);
    long a[6];
    for (int i = 0; i < 6; i++) a[i] = va_arg(ap, long);
    va_end(ap);
    if (number == SYS_getrandom && pinned_bytes((void *)a[0], (size_t)a[1]) == 0) return a[1];
    static long (*real)(long, ...) = NULL;
    if (!real) real = (long (*)(long, ...))dlsym(RTLD_NEXT, "syscall");
    return real(number, a[0], a[1], a[2], a[3], a[4], a[5]);
}

/* /dev/urandom and /dev/random: hand out a pipe fed from the pinned stream. */

static int is_random_dev(const char *path) {
    return path && (!strcmp(path, "/dev/urandom") || !strcmp(path, "/dev/random"));
}

static void *feeder(void *arg) {
    int fd = (int)(intptr_t)arg;
    uint8_t chunk[4096];
    for (;;) {
        fill(chunk, sizeof chunk);
        if (write(fd, chunk, sizeof chunk) < 0) break;  /* reader closed: EPIPE */
    }
    close(fd);
    return NULL;
}

/* Returns a readable fd, or -2 if not pinned (caller opens the real device). */
static int random_dev_fd(int flags) {
    uint8_t probe;
    if (pinned_bytes(&probe, 0) != 0) return -2;
    int p[2];
    if (pipe2(p, O_CLOEXEC) != 0) return -2;
    if (!(flags & O_CLOEXEC)) fcntl(p[0], F_SETFD, 0);
    /* The feeder must not die of SIGPIPE when the reader closes. */
    sigset_t block, old;
    sigemptyset(&block); sigaddset(&block, SIGPIPE);
    pthread_sigmask(SIG_BLOCK, &block, &old);
    pthread_t th;
    int rc = pthread_create(&th, NULL, feeder, (void *)(intptr_t)p[1]);
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    if (rc != 0) { close(p[0]); close(p[1]); return -2; }
    pthread_detach(th);
    return p[0];
}

typedef int (*open_fn)(const char *, int, ...);
typedef int (*openat_fn)(int, const char *, int, ...);
typedef FILE *(*fopen_fn)(const char *, const char *);
#define REAL(name, type) static type real_##name; if (!real_##name) real_##name = (type)dlsym(RTLD_NEXT, #name)

int open(const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); mode_t mode = va_arg(ap, mode_t); va_end(ap);
    if (is_random_dev(path)) { int fd = random_dev_fd(flags); if (fd != -2) return fd; }
    REAL(open, open_fn);
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); mode_t mode = va_arg(ap, mode_t); va_end(ap);
    if (is_random_dev(path)) { int fd = random_dev_fd(flags); if (fd != -2) return fd; }
    REAL(open64, open_fn);
    return real_open64(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); mode_t mode = va_arg(ap, mode_t); va_end(ap);
    if (is_random_dev(path)) { int fd = random_dev_fd(flags); if (fd != -2) return fd; }
    REAL(openat, openat_fn);
    return real_openat(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags); mode_t mode = va_arg(ap, mode_t); va_end(ap);
    if (is_random_dev(path)) { int fd = random_dev_fd(flags); if (fd != -2) return fd; }
    REAL(openat64, openat_fn);
    return real_openat64(dirfd, path, flags, mode);
}

FILE *fopen(const char *path, const char *mode) {
    if (is_random_dev(path)) {
        int fd = random_dev_fd(strchr(mode, 'e') ? O_CLOEXEC : 0);
        if (fd != -2) return fdopen(fd, "r");
    }
    REAL(fopen, fopen_fn);
    return real_fopen(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
    if (is_random_dev(path)) {
        int fd = random_dev_fd(strchr(mode, 'e') ? O_CLOEXEC : 0);
        if (fd != -2) return fdopen(fd, "r");
    }
    REAL(fopen64, fopen_fn);
    return real_fopen64(path, mode);
}

/* OpenSSL (shared libcrypto): its generator mixes pid, thread id and time
 * into the seed, so pinning the kernel source is not enough. */

typedef int (*rand_fn)(unsigned char *, int);
typedef int (*rand_ex_fn)(void *, unsigned char *, size_t, unsigned int);

static int rand_bytes(const char *name, unsigned char *p, int n) {
    if (n >= 0 && pinned_bytes(p, (size_t)n) == 0) return 1;
    rand_fn real = (rand_fn)dlsym(RTLD_NEXT, name);
    return real ? real(p, n) : 0;
}

static int rand_bytes_ex(const char *name, void *libctx, unsigned char *p, size_t n, unsigned int strength) {
    if (pinned_bytes(p, n) == 0) return 1;
    rand_ex_fn real = (rand_ex_fn)dlsym(RTLD_NEXT, name);
    return real ? real(libctx, p, n, strength) : 0;
}

int RAND_bytes(unsigned char *p, int n) { return rand_bytes("RAND_bytes", p, n); }
int RAND_priv_bytes(unsigned char *p, int n) { return rand_bytes("RAND_priv_bytes", p, n); }
int RAND_pseudo_bytes(unsigned char *p, int n) { return rand_bytes("RAND_pseudo_bytes", p, n); }
int RAND_bytes_ex(void *c, unsigned char *p, size_t n, unsigned int s) { return rand_bytes_ex("RAND_bytes_ex", c, p, n, s); }
int RAND_priv_bytes_ex(void *c, unsigned char *p, size_t n, unsigned int s) { return rand_bytes_ex("RAND_priv_bytes_ex", c, p, n, s); }
