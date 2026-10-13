/*
 * pyxbuild launcher stub for Linux (static, musl; built with `zig cc`).
 *
 * Same contract as stub/launcher.c: pyxbuild appends a manifest to a copy of
 * this stub,
 *
 *     [stub ELF image][manifest payload][u32 len][u32 version][8-byte magic]
 *
 * with UTF-8 "key=value" lines (python, flags, code), and the stub runs
 *
 *     <dir>/<python> <flags> -c <code> <launcher path> <original args>
 *
 * by exec(), so the interpreter replaces the launcher (signals, exit code and
 * the terminal go straight to Python).
 *
 * Onefile variant (-DPYX_ONEFILE): the application folder is appended as an
 * archive (src/pyxbuild/onefile.py) before the manifest, which adds
 * onefile/app/archive-offset/archive-size/extract like on Windows.
 * "cache" extracts once to $PYX_ONEFILE_DIR, else $XDG_CACHE_HOME/pyx, else
 * ~/.cache/pyx, as <app>/<hash12>: into a temporary folder under an exclusive
 * flock(), then renamed. Every running copy holds a shared flock on
 * <hash12>/.pyx-lock (inherited by Python through exec), so removing old
 * versions skips the ones in use; the newest previous version is kept.
 * "temp" extracts to $TMPDIR on every start, runs Python as a child and
 * deletes the folder afterwards. The archive stores no file modes: ELF files
 * and scripts ("#!") become executable.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PYX_MAGIC "PYXBLD01"
#define PYX_MAGIC_LEN 8
#define PYX_TRAILER_LEN (4 + 4 + PYX_MAGIC_LEN)
#define PYX_FORMAT_VERSION 1
#define PYX_MAX_PAYLOAD (1024 * 1024)

static const char *g_name = "pyxbuild launcher";

static void fail(const char *msg, const char *detail) {
    if (detail)
        fprintf(stderr, "%s: %s: %s\n", g_name, msg, detail);
    else
        fprintf(stderr, "%s: %s\n", g_name, msg);
    exit(127);
}

static void *xalloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) fail("out of memory", NULL);
    return p;
}

static char *join(const char *a, const char *b, const char *c) {
    size_t la = strlen(a), lb = strlen(b), lc = strlen(c);
    char *r = xalloc(la + lb + lc + 1);
    memcpy(r, a, la);
    memcpy(r + la, b, lb);
    memcpy(r + la + lb, c, lc + 1);
    return r;
}

static uint32_t le32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint64_t le64(const unsigned char *p) {
    return (uint64_t)le32(p) | ((uint64_t)le32(p + 4) << 32);
}

static int read_at(int fd, uint64_t off, void *buf, size_t len) {
    unsigned char *p = buf;
    while (len) {
        ssize_t n = pread(fd, p, len, (off_t)off);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        p += n;
        off += (uint64_t)n;
        len -= (size_t)n;
    }
    return 1;
}

static char *self_path(void) {
    char *buf = xalloc(PATH_MAX + 1);
    ssize_t n = readlink("/proc/self/exe", buf, PATH_MAX);
    if (n <= 0) fail("cannot find its own executable", NULL);
    buf[n] = 0;
    return buf;
}

/* manifest payload (NUL-terminated) */
static char *read_manifest(int fd) {
    struct stat st;
    unsigned char trailer[PYX_TRAILER_LEN];
    uint32_t len;
    char *payload;
    if (fstat(fd, &st) != 0 || st.st_size < PYX_TRAILER_LEN ||
        !read_at(fd, (uint64_t)st.st_size - PYX_TRAILER_LEN, trailer, PYX_TRAILER_LEN))
        fail("cannot read its manifest", NULL);
    if (memcmp(trailer + 8, PYX_MAGIC, PYX_MAGIC_LEN) != 0)
        fail("no application manifest (a bare launcher stub?)", NULL);
    if (le32(trailer + 4) != PYX_FORMAT_VERSION) fail("unsupported manifest version", NULL);
    len = le32(trailer);
    if (len == 0 || len > PYX_MAX_PAYLOAD || (off_t)len > st.st_size - PYX_TRAILER_LEN)
        fail("corrupt manifest", NULL);
    payload = xalloc(len + 1);
    if (!read_at(fd, (uint64_t)st.st_size - PYX_TRAILER_LEN - len, payload, len))
        fail("cannot read its manifest", NULL);
    payload[len] = 0;
    return payload;
}

static char *manifest_get(const char *payload, const char *key) {
    size_t klen = strlen(key);
    const char *p = payload;
    while (*p) {
        const char *eol = strchr(p, '\n');
        if (!eol) eol = p + strlen(p);
        if ((size_t)(eol - p) > klen && memcmp(p, key, klen) == 0 && p[klen] == '=') {
            size_t vlen = (size_t)(eol - p) - klen - 1;
            char *v = xalloc(vlen + 1);
            memcpy(v, p + klen + 1, vlen);
            v[vlen] = 0;
            return v;
        }
        p = *eol ? eol + 1 : eol;
    }
    return NULL;
}

static char *dirname_of(const char *path) {
    const char *slash = strrchr(path, '/');
    size_t n = slash ? (size_t)(slash - path) : 0;
    char *r = xalloc(n + 2);
    if (n == 0) {
        r[0] = slash ? '/' : '.';
        r[1] = 0;
    } else {
        memcpy(r, path, n);
        r[n] = 0;
    }
    return r;
}

/* argv for the interpreter: python, flags..., -c, code, exe, args... */
static char **build_argv(const char *python, const char *flags, const char *code,
                         const char *exe, int argc, char **argv) {
    char **out = xalloc(sizeof(char *) * (size_t)(argc + 64));
    int n = 0;
    char *f = flags ? strdup(flags) : NULL, *save = NULL, *tok;
    out[n++] = (char *)python;
    for (tok = f ? strtok_r(f, " ", &save) : NULL; tok && n < 60;
         tok = strtok_r(NULL, " ", &save))
        out[n++] = tok;
    out[n++] = "-c";
    out[n++] = (char *)code;
    out[n++] = (char *)exe;
    for (int i = 1; i < argc; i++) out[n++] = argv[i];
    out[n] = NULL;
    return out;
}

#ifdef PYX_ONEFILE
#include "inflate.h"

#define ARC_MAGIC "PYXARC01"
#define ARC_HEADER 32
#define ARC_ENTRY 16
#define ARC_RECORD 12
#define ARC_MAX_PATH 4096
#define STALE_SECONDS (10 * 60)

static pyx_inflater g_inflater;

static uint64_t parse_u64(const char *s) {
    uint64_t v = 0;
    if (!s || !*s) fail("corrupt onefile manifest", NULL);
    for (; *s; s++) {
        if (*s < '0' || *s > '9') fail("corrupt onefile manifest", NULL);
        v = v * 10 + (uint64_t)(*s - '0');
    }
    return v;
}

static void mkdirs(char *path) {
    for (char *p = path + 1; *p; p++) {
        if (*p != '/') continue;
        *p = 0;
        if (mkdir(path, 0755) != 0 && errno != EEXIST) fail("cannot create", path);
        *p = '/';
    }
    if (mkdir(path, 0755) != 0 && errno != EEXIST) fail("cannot create", path);
}

static int rmtree(const char *path) {
    struct stat st;
    if (lstat(path, &st) != 0) return errno == ENOENT;
    if (S_ISDIR(st.st_mode)) {
        DIR *d = opendir(path);
        struct dirent *e;
        if (!d) return 0;
        while ((e = readdir(d)) != NULL) {
            if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
            char *child = join(path, "/", e->d_name);
            rmtree(child);
            free(child);
        }
        closedir(d);
        return rmdir(path) == 0;
    }
    return unlink(path) == 0;
}

typedef struct {
    const char *dest;
    int out;
    uint64_t remaining;
    unsigned char hdr[ARC_RECORD];
    uint32_t hdrlen, pathlen, pathgot, files;
    int probed;
    char path[ARC_MAX_PATH];
} xstate;

static void open_entry(xstate *x) {
    uint32_t i, start = 0;
    char *full, *slash;
    /* relative, "/"-separated, no empty, "." or ".." segments */
    for (i = 0; i <= x->pathlen; i++) {
        if (i < x->pathlen) {
            char c = x->path[i];
            if (c == 0) fail("corrupt onefile archive", NULL);
            if (c != '/') continue;
        }
        if (i == start || (i - start == 1 && x->path[start] == '.') ||
            (i - start == 2 && x->path[start] == '.' && x->path[start + 1] == '.'))
            fail("corrupt onefile archive", NULL);
        start = i + 1;
    }
    x->path[x->pathlen] = 0;
    full = join(x->dest, "/", x->path);
    slash = strrchr(full, '/');
    *slash = 0;
    mkdirs(full);
    *slash = '/';
    x->out = open(full, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (x->out < 0) fail("cannot write", full);
    x->probed = 0;
    free(full);
}

static void close_entry(xstate *x) {
    close(x->out);
    x->out = -1;
    x->files++;
    x->hdrlen = x->pathgot = 0;
}

static void feed(xstate *x, const unsigned char *p, size_t len) {
    while (len) {
        size_t take;
        if (x->out < 0) {
            if (x->hdrlen < ARC_RECORD) {
                take = ARC_RECORD - x->hdrlen;
                if (take > len) take = len;
                memcpy(x->hdr + x->hdrlen, p, take);
                x->hdrlen += (uint32_t)take;
                p += take;
                len -= take;
                if (x->hdrlen < ARC_RECORD) return;
                x->pathlen = le32(x->hdr);
                x->remaining = le64(x->hdr + 4);
                x->pathgot = 0;
                if (x->pathlen == 0 || x->pathlen >= ARC_MAX_PATH)
                    fail("corrupt onefile archive", NULL);
            }
            take = x->pathlen - x->pathgot;
            if (take > len) take = len;
            memcpy(x->path + x->pathgot, p, take);
            x->pathgot += (uint32_t)take;
            p += take;
            len -= take;
            if (x->pathgot < x->pathlen) return;
            open_entry(x);
            if (x->remaining == 0) close_entry(x);
        } else {
            take = x->remaining < len ? (size_t)x->remaining : len;
            if (!x->probed) { /* executables: ELF images and scripts */
                x->probed = 1;
                if ((take >= 4 && memcmp(p, "\177ELF", 4) == 0) ||
                    (take >= 2 && p[0] == '#' && p[1] == '!'))
                    fchmod(x->out, 0755);
            }
            for (size_t done = 0; done < take;) {
                ssize_t n = write(x->out, p + done, take - done);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) fail("cannot write the extracted application (disk full?)", x->dest);
                done += (size_t)n;
            }
            x->remaining -= take;
            p += take;
            len -= take;
            if (x->remaining == 0) close_entry(x);
        }
    }
}

static void extract(int fd, uint64_t off, uint64_t size, const char *dest) {
    unsigned char hdr[ARC_HEADER];
    unsigned char *table, *cbuf, *ubuf;
    uint32_t chunks, files, i, maxc = 0, maxu = 0;
    xstate *x;
    if (size < ARC_HEADER || !read_at(fd, off, hdr, ARC_HEADER) ||
        memcmp(hdr, ARC_MAGIC, 8) != 0 || le32(hdr + 8) != 1)
        fail("corrupt onefile archive", NULL);
    chunks = le32(hdr + 12);
    files = le32(hdr + 24);
    if ((uint64_t)chunks * ARC_ENTRY > size - ARC_HEADER) fail("corrupt onefile archive", NULL);
    table = xalloc((size_t)chunks * ARC_ENTRY);
    if (chunks && !read_at(fd, off + ARC_HEADER, table, (size_t)chunks * ARC_ENTRY))
        fail("cannot read the onefile archive", NULL);
    for (i = 0; i < chunks; i++) {
        const unsigned char *e = table + (size_t)i * ARC_ENTRY;
        uint32_t c = le32(e + 8), u = le32(e + 12);
        if (le64(e) > size || c > size - le64(e) || c > (64u << 20) || u > (64u << 20))
            fail("corrupt onefile archive", NULL);
        if (c > maxc) maxc = c;
        if (u > maxu) maxu = u;
    }
    cbuf = xalloc(maxc);
    ubuf = xalloc(maxu);
    x = xalloc(sizeof(xstate));
    memset(x, 0, sizeof(xstate));
    x->dest = dest;
    x->out = -1;
    {
        char *d = strdup(dest);
        mkdirs(d);
        free(d);
    }
    for (i = 0; i < chunks; i++) {
        const unsigned char *e = table + (size_t)i * ARC_ENTRY;
        uint32_t c = le32(e + 8), u = le32(e + 12);
        if (!read_at(fd, off + le64(e), cbuf, c)) fail("cannot read the onefile archive", NULL);
        if (c == u)
            feed(x, cbuf, u); /* stored */
        else if (pyx_inflate(&g_inflater, cbuf, c, ubuf, u) == (long long)u)
            feed(x, ubuf, u);
        else
            fail("corrupt onefile archive (bad chunk)", NULL);
    }
    if (x->out >= 0 || x->hdrlen != 0 || x->files != files)
        fail("corrupt onefile archive (incomplete)", NULL);
    free(x);
    free(ubuf);
    free(cbuf);
    free(table);
}

static int exists(const char *p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static char *cache_base(void) {
    const char *v = getenv("PYX_ONEFILE_DIR");
    if (v && *v) return strdup(v);
    v = getenv("XDG_CACHE_HOME");
    if (v && *v == '/') return join(v, "/pyx", "");
    v = getenv("HOME");
    if (v && *v) return join(v, "/.cache/pyx", "");
    return join("/tmp/pyx-", getenv("USER") ? getenv("USER") : "user", "");
}

/* Remove other versions that no copy is running from; keep the newest one. */
static void sweep(const char *appdir, const char *keep) {
    DIR *d = opendir(appdir);
    struct dirent *e;
    char *newest = NULL;
    time_t newest_t = 0, now = time(NULL);
    if (!d) return;
    while ((e = readdir(d)) != NULL) { /* the newest other complete version */
        struct stat st;
        char *marker;
        if (e->d_name[0] == '.' || !strcmp(e->d_name, keep) || strlen(e->d_name) != 12) continue;
        marker = join(appdir, "/", e->d_name);
        char *m2 = join(marker, "/.pyx-complete", "");
        if (stat(m2, &st) == 0 && st.st_mtime >= newest_t) {
            newest_t = st.st_mtime;
            free(newest);
            newest = strdup(e->d_name);
        }
        free(m2);
        free(marker);
    }
    rewinddir(d);
    while ((e = readdir(d)) != NULL) {
        struct stat st;
        char *full;
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..") || !strcmp(e->d_name, keep))
            continue;
        full = join(appdir, "/", e->d_name);
        if (strstr(e->d_name, ".tmp-") || !strncmp(e->d_name, ".pyx-trash-", 11)) {
            if (lstat(full, &st) == 0 && now - st.st_mtime > STALE_SECONDS) rmtree(full);
        } else if (strlen(e->d_name) == 12 && (!newest || strcmp(e->d_name, newest))) {
            char *lock = join(full, "/.pyx-lock", "");
            int lfd = open(lock, O_RDONLY | O_CLOEXEC);
            if (lfd >= 0 && flock(lfd, LOCK_EX | LOCK_NB) == 0) {
                char trash[64];
                snprintf(trash, sizeof trash, "/.pyx-trash-%s-%ld", e->d_name, (long)getpid());
                char *t = join(appdir, trash, "");
                if (rename(full, t) == 0) rmtree(t);
                free(t);
            }
            if (lfd >= 0) close(lfd);
            free(lock);
        }
        free(full);
    }
    closedir(d);
    free(newest);
}

/* Shared lock on <root>/.pyx-lock, kept open (no O_CLOEXEC) through exec. */
static void hold_shared(const char *root) {
    char *lock = join(root, "/.pyx-lock", "");
    int fd = open(lock, O_RDWR | O_CREAT, 0644);
    if (fd >= 0) flock(fd, LOCK_SH);
    free(lock);
}

static pid_t g_child;

static void forward(int sig) {
    if (g_child > 0) kill(g_child, sig);
}

/* Returns the folder Python runs from; *temp: delete it after the run. */
static char *onefile_root(const char *exe, const char *payload, int *temp) {
    char *hash = manifest_get(payload, "onefile");
    char *app = manifest_get(payload, "app");
    char *mode = manifest_get(payload, "extract");
    uint64_t off = parse_u64(manifest_get(payload, "archive-offset"));
    uint64_t size = parse_u64(manifest_get(payload, "archive-size"));
    char short_hash[13], *base, *appdir, *root, *tmp, *lockpath, *marker, *reset;
    int fd, lfd;
    *temp = 0;
    if (!hash || strlen(hash) < 12 || !app || !*app || strchr(app, '/'))
        fail("corrupt onefile manifest", NULL);
    memcpy(short_hash, hash, 12);
    short_hash[12] = 0;
    fd = open(exe, O_RDONLY | O_CLOEXEC);
    if (fd < 0) fail("cannot open itself", exe);

    if (mode && !strcmp(mode, "temp")) {
        const char *t = getenv("TMPDIR");
        char name[96];
        snprintf(name, sizeof name, "/pyx-%.40s-%ld-%ld", app, (long)getpid(), (long)time(NULL));
        root = join(t && *t ? t : "/tmp", name, "");
        extract(fd, off, size, root);
        close(fd);
        *temp = 1;
        return root;
    }

    base = cache_base();
    appdir = join(base, "/", app);
    root = join(appdir, "/", short_hash);
    marker = join(root, "/.pyx-complete", "");
    reset = getenv("PYX_ONEFILE_RESET");
    if (exists(marker) && !(reset && *reset && strcmp(reset, "0"))) {
        close(fd);
        hold_shared(root);
        return root;
    }
    mkdirs(appdir);
    lockpath = join(appdir, "/.", short_hash);
    lockpath = join(lockpath, ".lock", "");
    lfd = open(lockpath, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
    if (lfd < 0 || flock(lfd, LOCK_EX) != 0) fail("cannot lock", lockpath);
    if (reset && *reset && strcmp(reset, "0") && exists(root)) {
        char trash[64];
        snprintf(trash, sizeof trash, "/.pyx-trash-%s-%ld", short_hash, (long)getpid());
        char *t = join(appdir, trash, "");
        if (rename(root, t) == 0) rmtree(t);
        free(t);
    }
    if (!exists(marker)) { /* nobody extracted it while we waited */
        char suffix[48];
        int mfd;
        snprintf(suffix, sizeof suffix, ".tmp-%ld-%ld", (long)getpid(), (long)time(NULL));
        tmp = join(root, suffix, "");
        extract(fd, off, size, tmp);
        {
            char *m = join(tmp, "/.pyx-complete", "");
            mfd = open(m, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
            if (mfd < 0) fail("cannot write", m);
            if (write(mfd, hash, strlen(hash)) < 0 || write(mfd, "\n", 1) < 0)
                fail("cannot write", m);
            close(mfd);
            free(m);
        }
        if (exists(root)) rmtree(root); /* an incomplete earlier attempt */
        if (rename(tmp, root) != 0) fail("cannot move the extracted application into place", root);
        free(tmp);
        hold_shared(root);
        sweep(appdir, short_hash);
    } else {
        hold_shared(root);
    }
    flock(lfd, LOCK_UN);
    close(lfd);
    close(fd);
    return root;
}
#endif /* PYX_ONEFILE */

int main(int argc, char **argv) {
    char *exe = self_path();
    int fd = open(exe, O_RDONLY | O_CLOEXEC);
    char *payload, *python, *flags, *code, *dir, *interp, **args;
    if (fd < 0) fail("cannot open itself", exe);
    payload = read_manifest(fd);
    close(fd);
    {
        char *app = manifest_get(payload, "app");
        if (app) g_name = app;
    }
    python = manifest_get(payload, "python");
    flags = manifest_get(payload, "flags");
    code = manifest_get(payload, "code");
    if (!python || !code) fail("incomplete manifest", NULL);
#ifdef PYX_ONEFILE
    int temp = 0;
    dir = onefile_root(exe, payload, &temp);
#else
    dir = dirname_of(exe);
#endif
    interp = join(dir, "/", python);
    args = build_argv(interp, flags, code, exe, argc, argv);
#ifdef PYX_ONEFILE
    if (temp) {
        int status = 0;
        g_child = fork();
        if (g_child < 0) fail("cannot start", interp);
        if (g_child == 0) {
            execv(interp, args);
            fail("cannot start the bundled Python", interp);
        }
        signal(SIGINT, SIG_IGN); /* the child gets Ctrl+C from the terminal */
        signal(SIGTERM, forward);
        signal(SIGHUP, forward);
        while (waitpid(g_child, &status, 0) < 0 && errno == EINTR) {
        }
        rmtree(dir);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        return WEXITSTATUS(status);
    }
#endif
    execv(interp, args);
    fail("cannot start the bundled Python", interp);
    return 127;
}
