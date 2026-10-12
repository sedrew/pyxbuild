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
 * The stub does not use the C runtime at all (only KERNEL32 and USER32), which
 * keeps it around 7 KB and free of UCRT/mingw start-up code.
 *
 * Onefile variant (-DPYX_ONEFILE): the application folder is appended as an
 * archive before the manifest. The manifest adds
 *
 *     onefile         sha256 of the archive (names the cache folder)
 *     app             application name (folder name)
 *     archive-offset  archive position in this file
 *     archive-size    archive length
 *     extract         "cache" (default) or "temp"
 *
 * and "python" is relative to the extracted folder. "cache" extracts once to
 * %LOCALAPPDATA%\pyx\<app>\<hash12> (or $PYX_ONEFILE_DIR\<app>\<hash12>):
 * into a temporary folder, then renamed, under a named mutex so concurrent
 * starts extract only once; later starts reuse it. Older versions are removed
 * (the newest previous one is kept). "temp" extracts to %TEMP% on every start
 * and deletes the folder when the application exits. See src/pyxbuild/onefile.py
 * for the archive format.
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

/* Skip argv[0] with the UCRT's rules: quotes toggle, there are no escapes in
 * the program name, and the name ends at the first unquoted blank. */
static const wchar_t *skip_program_name(const wchar_t *cmd) {
    BOOL quoted = FALSE;
    while (*cmd && (quoted || (*cmd != L' ' && *cmd != L'\t'))) {
        if (*cmd == L'"') quoted = !quoted;
        cmd++;
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

/* No C runtime: the stub only calls Win32. Tiny replacements for the few
 * helpers the compiler may emit calls to, and our own entry point. */
void *memcpy(void *dst, const void *src, size_t n) {
    unsigned char *d = dst;
    const unsigned char *s = src;
    while (n--) *d++ = *s++;
    return dst;
}

void *memset(void *dst, int c, size_t n) {
    unsigned char *d = dst;
    while (n--) *d++ = (unsigned char)c;
    return dst;
}

int memcmp(const void *a, const void *b, size_t n) {
    const unsigned char *x = a, *y = b;
    for (; n; n--, x++, y++)
        if (*x != *y) return *x - *y;
    return 0;
}


#ifdef PYX_ONEFILE
#include "inflate.h"

#define ARC_MAGIC "PYXARC01"
#define ARC_HEADER 32
#define ARC_ENTRY 16
#define ARC_RECORD 12
#define ARC_MAX_PATH 4096
#define STALE_100NS (10ULL * 60 * 10000000) /* 10 minutes */

static pyx_inflater g_inflater;

static void *xalloc(size_t n) {
    void *p = HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
    if (!p) fail(L"out of memory", NULL);
    return p;
}

static wchar_t *wjoin3(const wchar_t *a, const wchar_t *b, const wchar_t *c) {
    size_t la = lstrlenW(a), lb = lstrlenW(b), lc = lstrlenW(c);
    wchar_t *r = (wchar_t *)xalloc((la + lb + lc + 1) * sizeof(wchar_t));
    memcpy(r, a, la * sizeof(wchar_t));
    memcpy(r + la, b, lb * sizeof(wchar_t));
    memcpy(r + la + lb, c, (lc + 1) * sizeof(wchar_t));
    return r;
}

static ULONGLONG le64(const unsigned char *p) {
    return (ULONGLONG)le32(p) | ((ULONGLONG)le32(p + 4) << 32);
}

static ULONGLONG parse_u64(const wchar_t *s) {
    ULONGLONG v = 0;
    if (!s || !*s) fail(L"corrupt onefile manifest", NULL);
    for (; *s; s++) {
        if (*s < L'0' || *s > L'9') fail(L"corrupt onefile manifest", NULL);
        v = v * 10 + (ULONGLONG)(*s - L'0');
    }
    return v;
}

static BOOL exists(const wchar_t *p) {
    return GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES;
}

/* "\\?\" form of an absolute path: no MAX_PATH limit while extracting. */
static wchar_t *long_form(const wchar_t *p) {
    if (p[0] == L'\\' && p[1] == L'\\') {
        if (p[2] == L'?' || p[2] == L'.') return wjoin3(p, L"", L"");
        return wjoin3(L"\\\\?\\UNC", p + 1, L"");
    }
    return wjoin3(L"\\\\?\\", p, L"");
}

static void mkdirs(wchar_t *path) {
    wchar_t *p = path + 4; /* after "\\?\" */
    for (; *p; p++) {
        if (*p == L'\\') {
            *p = 0;
            CreateDirectoryW(path, NULL);
            *p = L'\\';
        }
    }
    CreateDirectoryW(path, NULL);
}

static void rmtree(const wchar_t *dir) {
    WIN32_FIND_DATAW fd;
    wchar_t *pattern = wjoin3(dir, L"\\*", L"");
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            wchar_t *child;
            if (!lstrcmpW(fd.cFileName, L".") || !lstrcmpW(fd.cFileName, L".."))
                continue;
            child = wjoin3(dir, L"\\", fd.cFileName);
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) &&
                !(fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
                rmtree(child);
            } else if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                RemoveDirectoryW(child); /* a junction: never follow it */
            } else {
                if (fd.dwFileAttributes & FILE_ATTRIBUTE_READONLY)
                    SetFileAttributesW(child, FILE_ATTRIBUTE_NORMAL);
                DeleteFileW(child);
            }
            HeapFree(GetProcessHeap(), 0, child);
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    HeapFree(GetProcessHeap(), 0, pattern);
    RemoveDirectoryW(dir);
}

/* Rename `dir` away first: Windows refuses while a program runs from it or
 * one of its files is open, so a folder in use is never deleted. */
static void trash(const wchar_t *dir, const wchar_t *parent) {
    wchar_t name[64];
    wchar_t *dest;
    wsprintfW(name, L"\\.pyx-trash-%lx-%lx", GetCurrentProcessId(), GetTickCount());
    dest = wjoin3(parent, name, L"");
    if (MoveFileExW(dir, dest, 0)) rmtree(dest);
    HeapFree(GetProcessHeap(), 0, dest);
}

static ULONGLONG ft64(FILETIME ft) {
    return ((ULONGLONG)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
}

static ULONGLONG now64(void) {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    return ft64(ft);
}

static BOOL starts_with(const wchar_t *s, const wchar_t *prefix) {
    while (*prefix)
        if (*s++ != *prefix++) return FALSE;
    return TRUE;
}

static BOOL contains(const wchar_t *s, const wchar_t *part) {
    for (; *s; s++)
        if (starts_with(s, part)) return TRUE;
    return FALSE;
}

/* Remove leftovers in `parent`, never a folder in use (see trash()).
 *
 * With `prefix` (temp mode, parent is %TEMP%): only our own folders, i.e.
 * names starting with `prefix` or ".pyx-trash-", older than ten minutes.
 * Without (cache mode, parent is the app's own cache folder): abandoned
 * ".tmp-"/".pyx-trash-" folders older than ten minutes, and every version
 * folder except `keep` and the newest other one. */
static void sweep(const wchar_t *parent, const wchar_t *prefix, const wchar_t *keep) {
    WIN32_FIND_DATAW fd;
    wchar_t *pattern = wjoin3(parent, L"\\*", L"");
    wchar_t newest[MAX_PATH];
    ULONGLONG newest_time = 0, now = now64();
    HANDLE h;
    int pass;
    newest[0] = 0;
    for (pass = 0; pass < 2; pass++) {
        h = FindFirstFileW(pattern, &fd);
        if (h == INVALID_HANDLE_VALUE) break;
        do {
            const wchar_t *n = fd.cFileName;
            BOOL is_trash = starts_with(n, L".pyx-trash-");
            BOOL old = now > ft64(fd.ftLastWriteTime) &&
                       now - ft64(fd.ftLastWriteTime) > STALE_100NS;
            BOOL remove = FALSE;
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
                !lstrcmpW(n, L".") || !lstrcmpW(n, L".."))
                continue;
            if (keep && !lstrcmpW(n, keep)) continue;
            if (prefix) {
                if (!is_trash && !starts_with(n, prefix)) continue;
                remove = pass == 1 && old;
            } else if (is_trash || contains(n, L".tmp-")) {
                remove = pass == 1 && old;
            } else if (pass == 0) {
                if (ft64(fd.ftLastWriteTime) >= newest_time && lstrlenW(n) < MAX_PATH) {
                    newest_time = ft64(fd.ftLastWriteTime);
                    lstrcpyW(newest, n);
                }
            } else {
                remove = lstrcmpW(n, newest) != 0;
            }
            if (remove) {
                wchar_t *path = wjoin3(parent, L"\\", n);
                trash(path, parent);
                HeapFree(GetProcessHeap(), 0, path);
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    HeapFree(GetProcessHeap(), 0, pattern);
}

#ifdef PYX_GUI
/* A small window while a GUI app is extracted for the first time (pythonw
 * shows nothing for seconds otherwise). Shown only when extracting takes
 * longer than PROGRESS_DELAY_MS; PYX_ONEFILE_PROGRESS=0 disables it, =1 shows
 * it at once. Manifest key "progress": absent = modern (drawn here in the
 * Windows 11 style: the app's icon and title, the user's accent color, light
 * or dark like the system, DPI aware, rounded corners on Windows 11),
 * "classic" (a plain STATIC window with the percentage) or "off". The
 * window never takes the focus. Only user32/gdi32 imports; dwmapi, advapi32
 * and ntdll functions are looked up at run time. */
#define PROGRESS_DELAY_MS 400
static HWND g_progress;
static int g_progress_mode = -1; /* -1 unknown, 0 off, 1 immediate, 2 delayed */
static int g_progress_classic;
static int g_progress_off;
static DWORD g_progress_start;
static const wchar_t *g_progress_app;
static const wchar_t *g_progress_title;

static struct {
    int dpi, permille, own_border;
    HFONT title_font, text_font;
    HICON icon;
    COLORREF bg, border, fg, fg2, track, fill, tile;
} g_pw;

static void progress_config(const wchar_t *style, const wchar_t *title) {
    g_progress_classic = style && !lstrcmpW(style, L"classic");
    g_progress_off = style && !lstrcmpW(style, L"off");
    g_progress_title = title;
}

static void progress_pump(void) {
    MSG m;
    while (PeekMessageW(&m, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&m);
        DispatchMessageW(&m);
    }
}

static void progress_begin(const wchar_t *app) {
    wchar_t v[4];
    DWORD n = GetEnvironmentVariableW(L"PYX_ONEFILE_PROGRESS", v, 4);
    g_progress_mode = (n == 1 && v[0] == L'0') ? 0 : (n == 1 && v[0] == L'1') ? 1
                      : g_progress_off ? 0 : 2;
    g_progress_start = GetTickCount();
    g_progress_app = app;
}

static HWND progress_classic(void) {
    int w = 380, h = 72;
    HWND hw = CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST, L"STATIC", L"",
        WS_POPUP | WS_BORDER | SS_CENTER | SS_CENTERIMAGE,
        (GetSystemMetrics(SM_CXSCREEN) - w) / 2, (GetSystemMetrics(SM_CYSCREEN) - h) / 2,
        w, h, NULL, NULL, NULL, NULL);
    if (hw) SendMessageW(hw, WM_SETFONT, (WPARAM)GetStockObject(DEFAULT_GUI_FONT), TRUE);
    return hw;
}

static int px(int v) { return MulDiv(v, g_pw.dpi, 96); }

typedef LONG(WINAPI *pyx_regget_fn)(HKEY, LPCWSTR, LPCWSTR, DWORD, LPDWORD, PVOID, LPDWORD);

/* HKCU value into buf; returns its size, 0 when absent */
static DWORD user_setting(const wchar_t *key, const wchar_t *name, void *buf, DWORD size) {
    static pyx_regget_fn get;
    static int tried;
    DWORD n = size;
    if (!tried) {
        HMODULE adv = LoadLibraryW(L"advapi32.dll");
        tried = 1;
        if (adv) get = (pyx_regget_fn)GetProcAddress(adv, "RegGetValueW");
    }
    if (!get || get(HKEY_CURRENT_USER, key, name, 0x0000ffff /* RRF_RT_ANY */, NULL, buf, &n))
        return 0;
    return n;
}

static void fill_rect(HDC dc, int l, int t, int r, int b, COLORREF c) {
    RECT rc;
    HBRUSH br = CreateSolidBrush(c);
    rc.left = l; rc.top = t; rc.right = r; rc.bottom = b;
    FillRect(dc, &rc, br);
    DeleteObject(br);
}

static void round_rect(HDC dc, int l, int t, int r, int b, int radius, COLORREF c) {
    HBRUSH br = CreateSolidBrush(c), ob = (HBRUSH)SelectObject(dc, br);
    HPEN op = (HPEN)SelectObject(dc, GetStockObject(NULL_PEN));
    RoundRect(dc, l, t, r + 1, b + 1, radius, radius); /* NULL_PEN: one pixel short */
    SelectObject(dc, ob);
    SelectObject(dc, op);
    DeleteObject(br);
}

static void text_at(HDC dc, const wchar_t *s, int l, int t, int r, int b, HFONT f,
                    COLORREF c, UINT align) {
    RECT rc;
    rc.left = l; rc.top = t; rc.right = r; rc.bottom = b;
    SelectObject(dc, f);
    SetTextColor(dc, c);
    DrawTextW(dc, s, -1, &rc, align | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS);
}

/* layout (at 96 dpi): 400 x 96, padding 20, icon 32, bar 3 px at y 72 */
static void progress_paint(HWND hw) {
    PAINTSTRUCT ps;
    RECT rc;
    HDC dc = BeginPaint(hw, &ps), mem;
    HBITMAP bmp, old;
    wchar_t pct[8], letter[2];
    const wchar_t *title = g_progress_title && *g_progress_title ? g_progress_title
                                                                 : g_progress_app;
    int w, h, x, iy, is, tx, bx, bw, by, bh, th, fw;
    GetClientRect(hw, &rc);
    w = rc.right;
    h = rc.bottom;
    mem = CreateCompatibleDC(dc);
    bmp = CreateCompatibleBitmap(dc, w, h);
    old = (HBITMAP)SelectObject(mem, bmp);
    SetBkMode(mem, TRANSPARENT);
    fill_rect(mem, 0, 0, w, h, g_pw.bg);
    if (g_pw.own_border) {
        fill_rect(mem, 0, 0, w, 1, g_pw.border);
        fill_rect(mem, 0, h - 1, w, h, g_pw.border);
        fill_rect(mem, 0, 0, 1, h, g_pw.border);
        fill_rect(mem, w - 1, 0, w, h, g_pw.border);
    }
    x = px(20);
    iy = px(22);
    is = px(32);
    if (g_pw.icon) {
        DrawIconEx(mem, x, iy, g_pw.icon, is, is, 0, NULL, DI_NORMAL);
    } else { /* no icon: a tile with the first letter */
        round_rect(mem, x, iy, x + is, iy + is, px(12), g_pw.tile);
        letter[0] = title[0];
        letter[1] = 0;
        CharUpperW(letter);
        text_at(mem, letter, x, iy, x + is, iy + is, g_pw.title_font, RGB(255, 255, 255),
                DT_CENTER);
    }
    tx = x + is + px(14);
    text_at(mem, title, tx, px(19), w - px(20), px(38), g_pw.title_font, g_pw.fg, DT_LEFT);
    wsprintfW(pct, L"%u%%", (unsigned)(g_pw.permille / 10));
    text_at(mem, pct, w - px(80), px(38), w - px(20), px(56), g_pw.text_font, g_pw.fg2,
            DT_RIGHT);
    text_at(mem, L"Preparing for first start\x2026", tx, px(38), w - px(80), px(56),
            g_pw.text_font, g_pw.fg2, DT_LEFT);
    bx = px(20);
    bw = w - 2 * bx;
    by = px(72);
    bh = px(3);
    th = px(1) ? px(1) : 1;
    fill_rect(mem, bx, by + (bh - th) / 2, bx + bw, by + (bh - th) / 2 + th, g_pw.track);
    fw = MulDiv(bw, g_pw.permille, 1000);
    if (fw > 0) round_rect(mem, bx, by, bx + fw, by + bh, bh, g_pw.fill);
    BitBlt(dc, 0, 0, w, h, mem, 0, 0, SRCCOPY);
    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
    EndPaint(hw, &ps);
}

static LRESULT CALLBACK progress_proc(HWND hw, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_PAINT: progress_paint(hw); return 0;
    case WM_ERASEBKGND: return 1;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    }
    return DefWindowProcW(hw, msg, wp, lp);
}

static HFONT ui_font(int size, int weight) {
    static const wchar_t *face;
    LOGFONTW lf;
    HFONT f;
    memset(&lf, 0, sizeof(lf));
    lf.lfHeight = -px(size);
    lf.lfWeight = weight;
    lf.lfCharSet = DEFAULT_CHARSET;
    lf.lfQuality = CLEARTYPE_QUALITY;
    if (!face) { /* Windows 11 ships Segoe UI Variable, older systems Segoe UI */
        wchar_t got[LF_FACESIZE];
        HDC dc = GetDC(NULL);
        face = L"Segoe UI";
        lstrcpyW(lf.lfFaceName, L"Segoe UI Variable Text");
        f = CreateFontIndirectW(&lf);
        if (dc && f) {
            HGDIOBJ o = SelectObject(dc, f);
            if (GetTextFaceW(dc, LF_FACESIZE, got) && !lstrcmpiW(got, lf.lfFaceName))
                face = L"Segoe UI Variable Text";
            SelectObject(dc, o);
        }
        if (dc) ReleaseDC(NULL, dc);
        if (f) DeleteObject(f);
    }
    lstrcpyW(lf.lfFaceName, face);
    return CreateFontIndirectW(&lf);
}

typedef BOOL(WINAPI *pyx_dpi_ctx_fn)(HANDLE);
typedef BOOL(WINAPI *pyx_dpi_fn)(void);
typedef LONG(WINAPI *pyx_version_fn)(OSVERSIONINFOW *);
typedef HRESULT(WINAPI *pyx_dwm_attr_fn)(HWND, DWORD, LPCVOID, DWORD);

static HWND progress_modern(void) {
    HMODULE user32 = GetModuleHandleW(L"user32.dll"), ntdll = GetModuleHandleW(L"ntdll.dll");
    pyx_dpi_ctx_fn dpi_ctx = (pyx_dpi_ctx_fn)GetProcAddress(user32, "SetProcessDpiAwarenessContext");
    pyx_dpi_fn dpi_old = (pyx_dpi_fn)GetProcAddress(user32, "SetProcessDPIAware");
    pyx_version_fn version = ntdll ? (pyx_version_fn)GetProcAddress(ntdll, "RtlGetVersion") : NULL;
    OSVERSIONINFOW osv;
    WNDCLASSEXW wc;
    DWORD light = 1, rounded = 0;
    unsigned char palette[32];
    HDC dc;
    HWND hw;
    int w, h;

    /* sharp on scaled displays (only this process; Python is a child) */
    if (!(dpi_ctx && dpi_ctx((HANDLE)-2 /* SYSTEM_AWARE */)) && dpi_old) dpi_old();
    g_pw.dpi = 96;
    dc = GetDC(NULL);
    if (dc) {
        g_pw.dpi = GetDeviceCaps(dc, LOGPIXELSY);
        ReleaseDC(NULL, dc);
    }
    if (g_pw.dpi < 96) g_pw.dpi = 96;

    /* light or dark, accent colors (defaults: Windows 11 blue) */
    user_setting(L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                 L"AppsUseLightTheme", &light, sizeof(light));
    g_pw.tile = RGB(0x00, 0x78, 0xd4);
    if (light) {
        g_pw.bg = RGB(0xfb, 0xfb, 0xfb); g_pw.border = RGB(0xd6, 0xd6, 0xd6);
        g_pw.fg = RGB(0x1a, 0x1a, 0x1a); g_pw.fg2 = RGB(0x5f, 0x5f, 0x5f);
        g_pw.track = RGB(0x8a, 0x8a, 0x8a); g_pw.fill = RGB(0x00, 0x5f, 0xb8);
    } else {
        g_pw.bg = RGB(0x2b, 0x2b, 0x2b); g_pw.border = RGB(0x48, 0x48, 0x48);
        g_pw.fg = RGB(0xff, 0xff, 0xff); g_pw.fg2 = RGB(0xc5, 0xc5, 0xc5);
        g_pw.track = RGB(0x9a, 0x9a, 0x9a); g_pw.fill = RGB(0x4c, 0xc2, 0xff);
    }
    /* AccentPalette: 8 x RGBA, lightest first; [1] light 2, [3] accent, [4] dark 1 */
    if (user_setting(L"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Accent",
                     L"AccentPalette", palette, sizeof(palette)) == sizeof(palette)) {
        const unsigned char *c = palette + (light ? 16 : 4);
        g_pw.fill = RGB(c[0], c[1], c[2]);
        g_pw.tile = RGB(palette[12], palette[13], palette[14]);
    }

    g_pw.title_font = ui_font(14, FW_SEMIBOLD);
    g_pw.text_font = ui_font(12, FW_NORMAL);
    g_pw.icon = (HICON)LoadImageW(GetModuleHandleW(NULL), MAKEINTRESOURCEW(1), IMAGE_ICON,
                                  px(32), px(32), LR_DEFAULTCOLOR);

    memset(&wc, 0, sizeof(wc));
    wc.cbSize = sizeof(wc);
    wc.style = CS_DROPSHADOW;
    wc.lpfnWndProc = progress_proc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_APPSTARTING);
    wc.lpszClassName = L"PyxProgress";
    RegisterClassExW(&wc);
    w = px(400);
    h = px(96);
    hw = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE, L"PyxProgress",
                         g_progress_title && *g_progress_title ? g_progress_title
                                                               : g_progress_app,
                         WS_POPUP, (GetSystemMetrics(SM_CXSCREEN) - w) / 2,
                         (GetSystemMetrics(SM_CYSCREEN) - h) / 2, w, h, NULL, NULL,
                         wc.hInstance, NULL);
    if (!hw) return NULL;

    /* Windows 11: rounded corners and the system border (DWMWA_WINDOW_CORNER_PREFERENCE) */
    memset(&osv, 0, sizeof(osv));
    osv.dwOSVersionInfoSize = sizeof(osv);
    if (version && !version(&osv) && osv.dwMajorVersion >= 10 && osv.dwBuildNumber >= 22000) {
        HMODULE dwm = LoadLibraryW(L"dwmapi.dll");
        pyx_dwm_attr_fn attr = dwm ? (pyx_dwm_attr_fn)GetProcAddress(dwm, "DwmSetWindowAttribute")
                                   : NULL;
        DWORD round = 2; /* DWMWCP_ROUND */
        rounded = attr && attr(hw, 33, &round, sizeof(round)) == S_OK;
    }
    g_pw.own_border = !rounded;
    return hw;
}

static void progress_update(ULONGLONG done, ULONGLONG total) {
    wchar_t text[300];
    int permille = (int)(total ? done * 1000 / total : 1000);
    if (g_progress_mode <= 0) return;
    if (!g_progress) {
        if (g_progress_mode == 2 && GetTickCount() - g_progress_start < PROGRESS_DELAY_MS)
            return;
        g_progress = g_progress_classic ? progress_classic() : progress_modern();
        if (!g_progress) { /* no desktop (service, headless): carry on silently */
            g_progress_mode = 0;
            return;
        }
        g_pw.permille = -1;
        ShowWindow(g_progress, SW_SHOWNOACTIVATE);
    }
    if (g_progress_classic) {
        wsprintfW(text, L"Preparing %.200s for its first start\x2026 %u%%", g_progress_app,
                  (unsigned)(permille / 10));
        SetWindowTextW(g_progress, text);
    } else if (permille != g_pw.permille) {
        g_pw.permille = permille;
        InvalidateRect(g_progress, NULL, FALSE);
    }
    UpdateWindow(g_progress);
    progress_pump();
}

static void progress_end(void) {
    if (g_progress) {
        DestroyWindow(g_progress);
        g_progress = NULL;
        progress_pump();
    }
}
#else
#define progress_begin(app) ((void)(app))
#define progress_config(style, title) ((void)(style), (void)(title))
#define progress_update(done, total) ((void)(done), (void)(total))
#define progress_end() ((void)0)
#endif

typedef struct {
    const wchar_t *dest; /* "\\?\" form */
    HANDLE out;
    ULONGLONG remaining;
    unsigned char hdr[ARC_RECORD];
    DWORD hdrlen, pathlen, pathgot, files;
    char path[ARC_MAX_PATH];
    wchar_t wpath[ARC_MAX_PATH + 1];
    wchar_t lastdir[ARC_MAX_PATH + 1];
} xstate;

static void open_entry(xstate *x) {
    wchar_t *wpath = x->wpath, *full, *slash;
    DWORD i, start = 0;
    int n;
    /* relative, "/"-separated, no empty, "." or ".." segments, no ':' or '\' */
    for (i = 0; i <= x->pathlen; i++) {
        if (i < x->pathlen) {
            char c = x->path[i];
            if (c == '\\' || c == ':' || c == 0) fail(L"corrupt onefile archive", NULL);
            if (c != '/') continue;
        }
        if (i == start || (i - start == 1 && x->path[start] == '.') ||
            (i - start == 2 && x->path[start] == '.' && x->path[start + 1] == '.'))
            fail(L"corrupt onefile archive", NULL);
        start = i + 1;
    }
    n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, x->path, (int)x->pathlen,
                            wpath, ARC_MAX_PATH);
    if (n <= 0) fail(L"corrupt onefile archive", NULL);
    wpath[n] = 0;
    for (i = 0; i < (DWORD)n; i++)
        if (wpath[i] == L'/') wpath[i] = L'\\';
    full = wjoin3(x->dest, L"\\", wpath);
    slash = full + lstrlenW(full);
    while (slash > full && *slash != L'\\') slash--;
    *slash = 0;
    if (lstrcmpW(full, x->lastdir) != 0) {
        mkdirs(full);
        if (lstrlenW(full) <= ARC_MAX_PATH) lstrcpyW(x->lastdir, full);
    }
    *slash = L'\\';
    x->out = CreateFileW(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, NULL);
    if (x->out == INVALID_HANDLE_VALUE) fail(L"cannot write", full);
    HeapFree(GetProcessHeap(), 0, full);
}

static void close_entry(xstate *x) {
    CloseHandle(x->out);
    x->out = NULL;
    x->files++;
    x->hdrlen = x->pathgot = 0;
}

static void feed(xstate *x, const unsigned char *p, DWORD len) {
    while (len) {
        DWORD take;
        if (x->out == NULL) {
            if (x->hdrlen < ARC_RECORD) {
                take = ARC_RECORD - x->hdrlen;
                if (take > len) take = len;
                memcpy(x->hdr + x->hdrlen, p, take);
                x->hdrlen += take;
                p += take;
                len -= take;
                if (x->hdrlen < ARC_RECORD) return;
                x->pathlen = le32(x->hdr);
                x->remaining = le64(x->hdr + 4);
                x->pathgot = 0;
                if (x->pathlen == 0 || x->pathlen >= ARC_MAX_PATH)
                    fail(L"corrupt onefile archive", NULL);
            }
            take = x->pathlen - x->pathgot;
            if (take > len) take = len;
            memcpy(x->path + x->pathgot, p, take);
            x->pathgot += take;
            p += take;
            len -= take;
            if (x->pathgot < x->pathlen) return;
            open_entry(x);
            if (x->remaining == 0) close_entry(x);
        } else {
            DWORD written;
            take = x->remaining < len ? (DWORD)x->remaining : len;
            if (!WriteFile(x->out, p, take, &written, NULL) || written != take)
                fail(L"cannot write the extracted application (disk full?)", x->dest);
            x->remaining -= take;
            p += take;
            len -= take;
            if (x->remaining == 0) close_entry(x);
        }
    }
}

static void extract(HANDLE f, ULONGLONG off, ULONGLONG size, const wchar_t *dest,
                    const wchar_t *app) {
    unsigned char hdr[ARC_HEADER];
    unsigned char *table, *cbuf, *ubuf;
    DWORD chunks, files, i, maxc = 0, maxu = 0;
    ULONGLONG total, done = 0;
    xstate *x;
    if (size < ARC_HEADER || !read_at(f, (LONGLONG)off, hdr, ARC_HEADER) ||
        memcmp(hdr, ARC_MAGIC, 8) != 0 || le32(hdr + 8) != 1)
        fail(L"corrupt onefile archive", NULL);
    chunks = le32(hdr + 12);
    total = le64(hdr + 16);
    files = le32(hdr + 24);
    if ((ULONGLONG)chunks * ARC_ENTRY > size - ARC_HEADER)
        fail(L"corrupt onefile archive", NULL);
    table = (unsigned char *)xalloc((size_t)chunks * ARC_ENTRY);
    if (chunks && !read_at(f, (LONGLONG)(off + ARC_HEADER), table, chunks * ARC_ENTRY))
        fail(L"cannot read the onefile archive", NULL);
    for (i = 0; i < chunks; i++) {
        const unsigned char *e = table + (size_t)i * ARC_ENTRY;
        DWORD c = le32(e + 8), u = le32(e + 12);
        if (le64(e) > size || c > size - le64(e) || c > 64u << 20 || u > 64u << 20)
            fail(L"corrupt onefile archive", NULL);
        if (c > maxc) maxc = c;
        if (u > maxu) maxu = u;
    }
    cbuf = (unsigned char *)xalloc(maxc);
    ubuf = (unsigned char *)xalloc(maxu);
    x = (xstate *)xalloc(sizeof(xstate));
    memset(x, 0, sizeof(xstate));
    x->dest = dest;
    mkdirs((wchar_t *)dest);
    progress_begin(app);
    for (i = 0; i < chunks; i++) {
        const unsigned char *e = table + (size_t)i * ARC_ENTRY;
        DWORD c = le32(e + 8), u = le32(e + 12);
        if (!read_at(f, (LONGLONG)(off + le64(e)), cbuf, c))
            fail(L"cannot read the onefile archive", NULL);
        if (c == u) {
            feed(x, cbuf, u); /* stored */
        } else if (pyx_inflate(&g_inflater, cbuf, c, ubuf, u) == (long long)u) {
            feed(x, ubuf, u);
        } else {
            fail(L"corrupt onefile archive (bad chunk)", NULL);
        }
        done += u;
        progress_update(done, total);
    }
    progress_end();
    if (x->out != NULL || x->hdrlen != 0 || x->files != files)
        fail(L"corrupt onefile archive (incomplete)", NULL);
    HeapFree(GetProcessHeap(), 0, x);
    HeapFree(GetProcessHeap(), 0, ubuf);
    HeapFree(GetProcessHeap(), 0, cbuf);
    HeapFree(GetProcessHeap(), 0, table);
}

static void write_marker(const wchar_t *dir, const wchar_t *hash) {
    wchar_t *path = wjoin3(dir, L"\\.pyx-complete", L"");
    char text[128];
    DWORD written;
    int n = WideCharToMultiByte(CP_UTF8, 0, hash, -1, text, sizeof(text) - 2, NULL, NULL);
    HANDLE h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) fail(L"cannot write", path);
    if (n > 0) {
        text[n - 1] = '\n';
        WriteFile(h, text, (DWORD)n, &written, NULL);
    }
    CloseHandle(h);
    HeapFree(GetProcessHeap(), 0, path);
}

static wchar_t *env(const wchar_t *name) {
    DWORD n = GetEnvironmentVariableW(name, NULL, 0);
    wchar_t *v;
    if (n == 0) return NULL;
    v = (wchar_t *)xalloc(n * sizeof(wchar_t));
    if (GetEnvironmentVariableW(name, v, n) == 0 || !*v) return NULL;
    return v;
}

static wchar_t *full_path(const wchar_t *p) {
    DWORD n = GetFullPathNameW(p, 0, NULL, NULL);
    wchar_t *r;
    if (n == 0) fail(L"invalid folder", p);
    r = (wchar_t *)xalloc((n + 1) * sizeof(wchar_t));
    GetFullPathNameW(p, n + 1, r, NULL);
    n = lstrlenW(r);
    while (n > 3 && (r[n - 1] == L'\\' || r[n - 1] == L'/')) r[--n] = 0;
    return r;
}

/* A patch (see src/pyxbuild/patch.py) assembles a newer version next to the
 * one it was made for and writes "<newer hash12>" into the older folder's
 * .pyx-next: an older executable then runs the newest patched version. */
static wchar_t *follow_next(const wchar_t *appdir, wchar_t *root) {
    int hops;
    for (hops = 0; hops < 16; hops++) {
        wchar_t *next_file = wjoin3(long_form(root), L"\\.pyx-next", L"");
        char text[16];
        wchar_t name[13], *candidate;
        DWORD got = 0;
        int i;
        HANDLE h = CreateFileW(next_file, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                               NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (h == INVALID_HANDLE_VALUE) break;
        if (!ReadFile(h, text, 12, &got, NULL)) got = 0;
        CloseHandle(h);
        if (got != 12) break;
        for (i = 0; i < 12; i++) {
            char c = text[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) break;
            name[i] = (wchar_t)c;
        }
        if (i != 12) break;
        name[12] = 0;
        candidate = wjoin3(appdir, L"\\", name);
        if (!exists(wjoin3(long_form(candidate), L"\\.pyx-complete", L"")))
            break; /* a patch that did not complete: keep this version */
        root = candidate;
    }
    return root;
}

static wchar_t *temp_folder(const wchar_t *app, const wchar_t *kind) {
    wchar_t tmp[MAX_PATH + 1], name[64];
    DWORD n = GetTempPathW(MAX_PATH + 1, tmp);
    wchar_t *prefix;
    if (n == 0 || n > MAX_PATH) fail(L"no temporary folder", NULL);
    if (n > 3 && tmp[n - 1] == L'\\') tmp[n - 1] = 0;
    prefix = wjoin3(L"pyx-", app, kind);
    sweep(tmp, prefix, NULL); /* folders left by killed runs */
    wsprintfW(name, L"%lx-%lx", GetCurrentProcessId(), GetTickCount());
    return wjoin3(wjoin3(tmp, L"\\", prefix), name, L"");
}

/* Extract (or find) the application; returns its folder (plain form) and
 * sets *temp_dir to the "\\?\" folder to delete after the run, if any. */
static wchar_t *onefile_root(const wchar_t *exe, const char *payload, wchar_t **temp_dir) {
    wchar_t *hash = manifest_get(payload, "onefile");
    wchar_t *app = manifest_get(payload, "app");
    wchar_t *mode = manifest_get(payload, "extract");
    wchar_t *patch_base = manifest_get(payload, "patch-base");
    ULONGLONG off = parse_u64(manifest_get(payload, "archive-offset"));
    ULONGLONG size = parse_u64(manifest_get(payload, "archive-size"));
    wchar_t short_hash[13];
    wchar_t *base, *appdir, *root, *xroot;
    HANDLE f;
    int i;
    *temp_dir = NULL;
    progress_config(manifest_get(payload, "progress"), manifest_get(payload, "title"));
    if (!hash || lstrlenW(hash) < 12 || !app || !*app)
        fail(L"corrupt onefile manifest", exe);
    for (i = 0; i < 12; i++) short_hash[i] = hash[i];
    short_hash[12] = 0;
    f = CreateFileW(exe, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, NULL,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) fail(L"cannot open itself", exe);

    if (mode && !lstrcmpW(mode, L"temp") && !patch_base) {
        root = temp_folder(app, L"-");
        xroot = long_form(root);
        extract(f, off, size, xroot, app);
        CloseHandle(f);
        *temp_dir = xroot;
        return root;
    }

    base = env(L"PYX_ONEFILE_DIR");
    if (base) {
        base = full_path(base);
    } else {
        wchar_t *local = env(L"LOCALAPPDATA");
        if (!local) {
            wchar_t tmp[MAX_PATH + 1];
            if (GetTempPathW(MAX_PATH + 1, tmp) == 0) fail(L"no folder to extract to", NULL);
            local = full_path(tmp);
        }
        base = wjoin3(local, L"\\pyx", L"");
    }
    appdir = wjoin3(base, L"\\", app);

    if (patch_base) {
        /* An update: the archive holds the patch; the installed version it
         * was made for runs _pyxbuild_patch from it. */
        wchar_t base_hash[13], *xtmp, *tmp;
        if (lstrlenW(patch_base) < 12) fail(L"corrupt patch manifest", exe);
        for (i = 0; i < 12; i++) base_hash[i] = patch_base[i];
        base_hash[12] = 0;
        root = wjoin3(appdir, L"\\", base_hash);
        if (!exists(wjoin3(long_form(root), L"\\.pyx-complete", L"")))
            fail(L"this update is for a version of the application that is not installed "
                 L"here; start the application once, or install the full version",
                 root);
        tmp = temp_folder(app, L"-patch-");
        xtmp = long_form(tmp);
        extract(f, off, size, xtmp, app);
        CloseHandle(f);
        SetEnvironmentVariableW(L"PYX_PATCH_PAYLOAD", tmp);
        SetEnvironmentVariableW(L"PYX_PATCH_APPDIR", appdir);
        SetEnvironmentVariableW(L"PYX_PATCH_EXE", exe);
        *temp_dir = xtmp;
        return root;
    }

    root = wjoin3(appdir, L"\\", short_hash);
    xroot = long_form(root);
    {
        wchar_t *xappdir = long_form(appdir);
        wchar_t *marker = wjoin3(xroot, L"\\.pyx-complete", L"");
        wchar_t *reset = env(L"PYX_ONEFILE_RESET");
        if (reset && lstrcmpW(reset, L"0") != 0 && exists(xroot)) trash(xroot, xappdir);
        if (!exists(marker)) {
            wchar_t mutex_name[64], suffix[64];
            HANDLE mutex;
            wsprintfW(mutex_name, L"Local\\pyx-onefile-%s", short_hash);
            mkdirs(xappdir);
            mutex = CreateMutexW(NULL, FALSE, mutex_name);
            if (mutex) WaitForSingleObject(mutex, INFINITE);
            if (!exists(marker)) { /* another start may have finished meanwhile */
                wchar_t *xtmp;
                if (exists(xroot)) trash(xroot, xappdir); /* incomplete */
                wsprintfW(suffix, L".tmp-%lx-%lx", GetCurrentProcessId(), GetTickCount());
                xtmp = wjoin3(xroot, suffix, L"");
                extract(f, off, size, xtmp, app);
                write_marker(xtmp, hash);
                if (!MoveFileExW(xtmp, xroot, 0)) {
                    if (!exists(marker)) fail(L"cannot install the extracted application", root);
                    rmtree(xtmp);
                }
                sweep(xappdir, NULL, short_hash); /* older versions */
            }
            if (mutex) {
                ReleaseMutex(mutex);
                CloseHandle(mutex);
            }
        }
    }
    CloseHandle(f);
    return follow_next(appdir, root);
}
#endif /* PYX_ONEFILE */

static int run(void);

void __stdcall pyx_entry(void) { ExitProcess((UINT)run()); }

static int run(void) {
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
#ifdef PYX_ONEFILE
    wchar_t *temp_dir = NULL;
#endif

    if (!python || !code) fail(L"manifest lacks 'python' or 'code'", exe);
    if (!flags) flags = L"";

#ifdef PYX_ONEFILE
    /* the extracted application folder, with a trailing backslash */
    dir = onefile_root(exe, payload, &temp_dir);
    dir = wjoin3(dir, L"\\", L"");
    dlen = (size_t)lstrlenW(dir);
    (void)slash;
#else
    /* directory of the launcher, including the trailing backslash */
    slash = exe + lstrlenW(exe);
    while (slash > exe && slash[-1] != L'\\' && slash[-1] != L'/') slash--;
    dlen = (size_t)(slash - exe);
    dir = exe;
#endif

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
    /* Start suspended so the child is in the job before it runs: if the
     * launcher dies, the job closes and takes the child with it. */
    if (!CreateProcessW(interp, cmdline, NULL, NULL, TRUE, CREATE_SUSPENDED, NULL,
                        NULL, &si, &pi))
        fail(L"cannot start the Python runtime", interp);
    if (job) AssignProcessToJobObject(job, pi.hProcess);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
#ifdef PYX_GUI
    {
        /* Explorer shows the "app starting" cursor until the launched process
         * takes a message from its queue; ours never would (CPython's
         * launcher does the same, bpo-17290). */
        MSG msg;
        PostMessageW(NULL, 0, 0, 0);
        GetMessageW(&msg, NULL, 0, 0);
    }
#endif
    WaitForSingleObject(pi.hProcess, INFINITE);
    if (!GetExitCodeProcess(pi.hProcess, &exit_code)) exit_code = 1;
    CloseHandle(pi.hProcess);
#ifdef PYX_ONEFILE
    if (temp_dir) {
        /* "temp" mode: the files are released once the process has exited;
         * a folder still in use (a detached child) is swept on a later start */
        int tries;
        for (tries = 0; tries < 5 && exists(temp_dir); tries++) {
            rmtree(temp_dir);
            if (exists(temp_dir)) Sleep(200);
        }
    }
#endif
    ExitProcess(exit_code);
    return (int)exit_code;
}
