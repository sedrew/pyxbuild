/* Host harness for stub/inflate.h: inflate_main IN OUT EXPECTED_SIZE */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../stub/inflate.h"

int main(int argc, char **argv) {
    FILE *f;
    long n;
    unsigned char *in, *out;
    size_t cap;
    long long got;
    static pyx_inflater z;
    if (argc != 4) return 2;
    f = fopen(argv[1], "rb");
    if (!f) return 2;
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    in = malloc(n ? n : 1);
    if (fread(in, 1, n, f) != (size_t)n) return 2;
    fclose(f);
    cap = (size_t)atol(argv[3]);
    out = malloc(cap ? cap : 1);
    got = pyx_inflate(&z, in, (size_t)n, out, cap);
    if (got < 0) {
        printf("error %lld\n", got);
        return 1;
    }
    f = fopen(argv[2], "wb");
    fwrite(out, 1, (size_t)got, f);
    fclose(f);
    printf("ok %lld\n", got);
    return 0;
}
