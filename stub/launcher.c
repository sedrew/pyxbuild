/*
 * pyxbuild launcher stub for Windows.
 *
 * The stub is a tiny, generic executable. Everything application-specific
 * lives in a manifest that pyxbuild appends to a copy of the stub:
 *
 *     [stub PE image][manifest payload][u32 len][u32 version][8-byte magic]
 *
 * The payload is UTF-8 text, one "key=value" pair per line. Known keys:
 *
 *     python  interpreter path relative to the launcher's directory
 *     flags   interpreter flags placed before "-c" (e.g. "-I")
 *     code    Python source passed with "-c"; must not contain '"'
 *
 * The launcher then runs
 *
 *     "<dir>\<python>" <flags> -c "<code>" "<launcher path>" <original args>
 *
 * as a child process inside a job object, forwards its exit code, and
 * ignores console control events itself so that Ctrl+C reaches the child.
 *
 * If the file carries an Authenticode signature, the certificate table is
 * appended after our trailer; the security data directory tells us where the
 * original overlay ends, and up to 7 bytes of zero padding are skipped.
 *
 * Build: python tools/build_stubs.py (uses `zig cc`, see that script).
 */

#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>

#define PYX_MAGIC "PYXBLD01"
#define PYX_MAGIC_LEN 8
#define PYX_TRAILER_LEN (4 + 4 + PYX_MAGIC_LEN)
#define PYX_FORMAT_VERSION 1
#define PYX_MAX_PAYLOAD (1024 * 1024)

static void fail(const wchar_t *msg, const wchar_t *detail) {
    wchar_t buf[2048];
    DWORD err = GetLastError();
    wsprintfW(buf, L"pyxbuild launcher: %s%s%s (error %lu)", msg,
              detail ? L": " : L"", detail ? detail : L"", err);
#ifdef PYX_GUI
    MessageBoxW(NULL, buf, L"Application error", MB_OK | MB_ICONERROR);
#else
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    DWORD written;
    if (h != NULL && h != INVALID_HANDLE_VALUE) {
        DWORD mode;
        if (GetConsoleMode(h, &mode)) {
            WriteConsoleW(h, buf, lstrlenW(buf), &written, NULL);
            WriteConsoleW(h, L"\r\n", 2, &written, NULL);
        } else {
            char out[4096];
            int n = WideCharToMultiByte(CP_UTF8, 0, buf, -1, out,
                                        sizeof(out) - 2, NULL, NULL);
            if (n > 0) {
                out[n - 1] = '\n';
                WriteFile(h, out, n, &written, NULL);
            }
        }
    }
#endif
    ExitProcess(101);
}

static BOOL WINAPI ctrl_handler(DWORD type) {
    (void)type;
    return TRUE; /* the child receives the event too and decides */
}

static HANDLE inheritable(HANDLE h) {
    if (h != NULL && h != INVALID_HANDLE_VALUE)
        SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    return h;
}

static BOOL read_at(HANDLE f, LONGLONG off, void *buf, DWORD len) {
    LARGE_INTEGER li;
    DWORD got = 0;
    li.QuadPart = off;
    if (!SetFilePointerEx(f, li, NULL, FILE_BEGIN)) return FALSE;
    return ReadFile(f, buf, len, &got, NULL) && got == len;
}

static DWORD le32(const unsigned char *p) {
    return (DWORD)p[0] | ((DWORD)p[1] << 8) | ((DWORD)p[2] << 16) |
           ((DWORD)p[3] << 24);
}

/* End offset of the data we appended: file size, or the start of the
 * Authenticode certificate table if the file is signed. */
static LONGLONG overlay_end(HANDLE f, LONGLONG size) {
    unsigned char hdr[4], opt[2], dir[8];
    DWORD pe_off, sec_off;
    LONGLONG dirs_off;
    if (!read_at(f, 0x3C, hdr, 4)) return size;
    pe_off = le32(hdr);
    if (!read_at(f, (LONGLONG)pe_off + 24, opt, 2)) return size;
    /* PE32+ optional header: data directories start at offset 112 */
    if (opt[0] != 0x0B || opt[1] != 0x02) return size;
    dirs_off = (LONGLONG)pe_off + 24 + 112;
    if (!read_at(f, dirs_off + 4 * 8, dir, 8)) return size; /* SECURITY */
    sec_off = le32(dir);
    if (sec_off == 0 || le32(dir + 4) == 0 || sec_off > size) return size;
    return sec_off;
}

static char *read_manifest(const wchar_t *path) {
    HANDLE f;
    LARGE_INTEGER size;
    LONGLONG end;
    unsigned char trailer[PYX_TRAILER_LEN];
    unsigned char probe;
    DWORD len;
    char *payload;
    int pad;

    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) fail(L"cannot open itself", path);
    if (!GetFileSizeEx(f, &size)) fail(L"cannot stat itself", path);
    end = overlay_end(f, size.QuadPart);
    /* Signing tools align the certificate table to 8 bytes with zeros. */
    for (pad = 0; pad < 8 && end > 0; pad++) {
        if (!read_at(f, end - 1, &probe, 1)) break;
        if (probe != 0) break;
        end--;
    }
    if (end < PYX_TRAILER_LEN || !read_at(f, end - PYX_TRAILER_LEN, trailer,
                                          PYX_TRAILER_LEN))
        fail(L"no application manifest found", path);
    if (memcmp(trailer + 8, PYX_MAGIC, PYX_MAGIC_LEN) != 0)
        fail(L"no application manifest found", path);
    if (le32(trailer + 4) != PYX_FORMAT_VERSION)
        fail(L"unsupported manifest version", path);
    len = le32(trailer);
    if (len == 0 || len > PYX_MAX_PAYLOAD ||
        (LONGLONG)len > end - PYX_TRAILER_LEN)
        fail(L"corrupt application manifest", path);
    payload = (char *)HeapAlloc(GetProcessHeap(), 0, len + 1);
    if (!payload) fail(L"out of memory", NULL);
    if (!read_at(f, end - PYX_TRAILER_LEN - len, payload, len))
        fail(L"cannot read application manifest", path);
    payload[len] = 0;
    CloseHandle(f);
    return payload;
}

/* Returns a newly allocated UTF-16 copy of the value for `key`, or NULL. */
static wchar_t *manifest_get(const char *payload, const char *key) {
    size_t klen = lstrlenA(key);
    const char *p = payload;
    while (*p) {
        const char *eol = p;
        while (*eol && *eol != '\n') eol++;
        if ((size_t)(eol - p) > klen && memcmp(p, key, klen) == 0 &&
            p[klen] == '=') {
            const char *v = p + klen + 1;
            int vlen = (int)(eol - v);
            int wlen;
            wchar_t *w;
            if (vlen > 0 && v[vlen - 1] == '\r') vlen--;
            wlen = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, v, vlen,
                                       NULL, 0);
            if (vlen > 0 && wlen <= 0) fail(L"manifest is not UTF-8", NULL);
            w = (wchar_t *)HeapAlloc(GetProcessHeap(), 0,
                                     (wlen + 1) * sizeof(wchar_t));
            if (!w) fail(L"out of memory", NULL);
            if (vlen > 0) MultiByteToWideChar(CP_UTF8, 0, v, vlen, w, wlen);
            w[wlen] = 0;
            return w;
        }
        p = *eol ? eol + 1 : eol;
    }
    return NULL;
}

/* Skip argv[0] using the same rules as the MSVC runtime. */
static const wchar_t *skip_program_name(const wchar_t *cmd) {
    if (*cmd == L'"') {
        cmd++;
        while (*cmd && *cmd != L'"') cmd++;
        if (*cmd == L'"') cmd++;
    } else {
        while (*cmd && *cmd != L' ' && *cmd != L'\t') cmd++;
    }
    while (*cmd == L' ' || *cmd == L'\t') cmd++;
    return cmd;
}

static wchar_t *self_path(void) {
    DWORD cap = MAX_PATH, n;
    wchar_t *buf;
    for (;;) {
        buf = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, cap * sizeof(wchar_t));
        if (!buf) fail(L"out of memory", NULL);
        n = GetModuleFileNameW(NULL, buf, cap);
        if (n == 0) fail(L"cannot determine own path", NULL);
        if (n < cap) return buf;
        HeapFree(GetProcessHeap(), 0, buf);
        if (cap >= 32768) fail(L"own path too long", NULL);
        cap *= 2;
    }
}

int main(void) {
    wchar_t *exe = self_path();
    char *payload = read_manifest(exe);
    wchar_t *python = manifest_get(payload, "python");
    wchar_t *flags = manifest_get(payload, "flags");
    wchar_t *code = manifest_get(payload, "code");
    const wchar_t *rest = skip_program_name(GetCommandLineW());
    wchar_t *slash, *dir, *interp, *cmdline;
    size_t dlen, ilen, clen;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    HANDLE job;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jl;
    DWORD exit_code = 1;

    if (!python || !code) fail(L"manifest lacks 'python' or 'code'", exe);
    if (!flags) flags = L"";

    /* directory of the launcher, including the trailing backslash */
    slash = exe + lstrlenW(exe);
    while (slash > exe && slash[-1] != L'\\' && slash[-1] != L'/') slash--;
    dlen = (size_t)(slash - exe);
    dir = exe;

    ilen = dlen + lstrlenW(python) + 1;
    interp = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, ilen * sizeof(wchar_t));
    if (!interp) fail(L"out of memory", NULL);
    memcpy(interp, dir, dlen * sizeof(wchar_t));
    lstrcpyW(interp + dlen, python);
    if (GetFileAttributesW(interp) == INVALID_FILE_ATTRIBUTES)
        fail(L"bundled Python runtime not found", interp);

    clen = ilen + lstrlenW(flags) + lstrlenW(code) + lstrlenW(exe) +
           lstrlenW(rest) + 32;
    cmdline = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, clen * sizeof(wchar_t));
    if (!cmdline) fail(L"out of memory", NULL);
    lstrcpyW(cmdline, L"\"");
    lstrcatW(cmdline, interp);
    lstrcatW(cmdline, L"\" ");
    if (*flags) {
        lstrcatW(cmdline, flags);
        lstrcatW(cmdline, L" ");
    }
    lstrcatW(cmdline, L"-c \"");
    lstrcatW(cmdline, code);
    lstrcatW(cmdline, L"\" \"");
    lstrcatW(cmdline, exe);
    lstrcatW(cmdline, L"\"");
    if (*rest) {
        lstrcatW(cmdline, L" ");
        lstrcatW(cmdline, rest);
    }

    job = CreateJobObjectW(NULL, NULL);
    if (job) {
        ZeroMemory(&jl, sizeof(jl));
        jl.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
            JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
        SetInformationJobObject(job, JobObjectExtendedLimitInformation, &jl,
                                sizeof(jl));
    }

    SetConsoleCtrlHandler(ctrl_handler, TRUE);

    /* Pass our standard handles explicitly: they must be inheritable. */
    {
        STARTUPINFOW parent;
        GetStartupInfoW(&parent);
        ZeroMemory(&si, sizeof(si));
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        if (parent.dwFlags & STARTF_USESHOWWINDOW) {
            si.dwFlags |= STARTF_USESHOWWINDOW;
            si.wShowWindow = parent.wShowWindow;
        }
        si.hStdInput = inheritable(GetStdHandle(STD_INPUT_HANDLE));
        si.hStdOutput = inheritable(GetStdHandle(STD_OUTPUT_HANDLE));
        si.hStdError = inheritable(GetStdHandle(STD_ERROR_HANDLE));
    }
    if (!CreateProcessW(interp, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si,
                        &pi))
        fail(L"cannot start the Python runtime", interp);
    if (job) AssignProcessToJobObject(job, pi.hProcess);
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    if (!GetExitCodeProcess(pi.hProcess, &exit_code)) exit_code = 1;
    CloseHandle(pi.hProcess);
    ExitProcess(exit_code);
    return (int)exit_code;
}
