/*
 * Raw DEFLATE (RFC 1951) decoder for the onefile launcher.
 *
 * Buffer to buffer: the whole compressed chunk is in memory and the output
 * size is known, which keeps the code short. Huffman codes up to 9 bits are
 * decoded with a lookup table, longer ones with the canonical-code slow path.
 * No allocation, no C runtime beyond memset/memcpy (the launcher provides
 * them). tests/test_inflate.py compiles this file for the build host and
 * compares it with zlib on random, real and corrupted data.
 */

#ifndef PYX_INFLATE_H
#define PYX_INFLATE_H

#include <stddef.h>

#define PYX_ZFAST_BITS 9
#define PYX_ZFAST_MASK ((1 << PYX_ZFAST_BITS) - 1)

typedef struct {
    unsigned short fast[1 << PYX_ZFAST_BITS];
    unsigned short firstcode[16];
    int maxcode[17];
    unsigned short firstsymbol[16];
    unsigned char size[288];
    unsigned short value[288];
} pyx_huffman;

typedef struct {
    const unsigned char *in;
    size_t inlen;
    size_t inpos;          /* bytes taken from `in`; may run past inlen (zeros) */
    unsigned int bits;     /* bit buffer, LSB first */
    int nbits;
    unsigned char *out;
    size_t outlen;
    size_t outpos;
    pyx_huffman lit, dist;
} pyx_inflater;

static int pyx_bitrev(int v, int n) {
    int r = 0;
    while (n--) {
        r = (r << 1) | (v & 1);
        v >>= 1;
    }
    return r;
}

static void pyx_fill(pyx_inflater *z) {
    while (z->nbits <= 24) {
        unsigned int b = z->inpos < z->inlen ? z->in[z->inpos] : 0;
        z->inpos++;
        z->bits |= b << z->nbits;
        z->nbits += 8;
    }
}

static int pyx_overrun(const pyx_inflater *z) {
    /* bits consumed beyond the real input */
    return (z->inpos * 8 - (size_t)z->nbits) > z->inlen * 8;
}

static unsigned int pyx_take(pyx_inflater *z, int n) {
    unsigned int v;
    if (z->nbits < n) pyx_fill(z);
    v = z->bits & ((1u << n) - 1);
    z->bits >>= n;
    z->nbits -= n;
    return v;
}

/* Build the decoding tables from code lengths; 0 on an invalid code set. */
static int pyx_build(pyx_huffman *h, const unsigned char *lengths, int num) {
    int i, k = 0, code = 0;
    int next[16], count[17];
    memset(count, 0, sizeof(count));
    memset(h->fast, 0, sizeof(h->fast));
    for (i = 0; i < num; i++) count[lengths[i]]++;
    count[0] = 0;
    for (i = 1; i < 16; i++)
        if (count[i] > (1 << i)) return 0;
    for (i = 1; i < 16; i++) {
        next[i] = code;
        h->firstcode[i] = (unsigned short)code;
        h->firstsymbol[i] = (unsigned short)k;
        code += count[i];
        if (count[i] && code - 1 >= (1 << i)) return 0; /* over-subscribed */
        h->maxcode[i] = code << (16 - i);
        code <<= 1;
        k += count[i];
    }
    h->maxcode[16] = 0x10000;
    for (i = 0; i < num; i++) {
        int s = lengths[i];
        if (s) {
            int c = next[s] - h->firstcode[s] + h->firstsymbol[s];
            unsigned short entry = (unsigned short)((s << 9) | i);
            h->size[c] = (unsigned char)s;
            h->value[c] = (unsigned short)i;
            if (s <= PYX_ZFAST_BITS) {
                int j = pyx_bitrev(next[s], s);
                while (j < (1 << PYX_ZFAST_BITS)) {
                    h->fast[j] = entry;
                    j += 1 << s;
                }
            }
            next[s]++;
        }
    }
    return 1;
}

static int pyx_decode(pyx_inflater *z, const pyx_huffman *h) {
    int b, s, k;
    if (z->nbits < 16) pyx_fill(z);
    b = h->fast[z->bits & PYX_ZFAST_MASK];
    if (b) {
        s = b >> 9;
        z->bits >>= s;
        z->nbits -= s;
        return b & 511;
    }
    k = pyx_bitrev((int)(z->bits & 0xFFFF), 16);
    for (s = PYX_ZFAST_BITS + 1; s < 16; s++)
        if (k < h->maxcode[s]) break;
    if (s >= 16) return -1;
    b = (k >> (16 - s)) - h->firstcode[s] + h->firstsymbol[s];
    if (b < 0 || b >= 288 || h->size[b] != s) return -1;
    z->bits >>= s;
    z->nbits -= s;
    return h->value[b];
}

static const unsigned short pyx_len_base[29] = {
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59,
    67, 83, 99, 115, 131, 163, 195, 227, 258};
static const unsigned char pyx_len_extra[29] = {
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3,
    4, 4, 4, 4, 5, 5, 5, 5, 0};
static const unsigned short pyx_dist_base[30] = {
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513,
    769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577};
static const unsigned char pyx_dist_extra[30] = {
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
    11, 11, 12, 12, 13, 13};

static int pyx_codes(pyx_inflater *z) {
    for (;;) {
        int sym = pyx_decode(z, &z->lit);
        if (sym < 0) return -3;
        if (sym < 256) {
            if (z->outpos >= z->outlen) return -4;
            z->out[z->outpos++] = (unsigned char)sym;
        } else if (sym == 256) {
            return pyx_overrun(z) ? -5 : 0;
        } else {
            size_t len, d;
            sym -= 257;
            if (sym >= 29) return -3;
            len = pyx_len_base[sym] + pyx_take(z, pyx_len_extra[sym]);
            sym = pyx_decode(z, &z->dist);
            if (sym < 0 || sym >= 30) return -3;
            d = pyx_dist_base[sym] + pyx_take(z, pyx_dist_extra[sym]);
            if (d > z->outpos) return -6;
            if (len > z->outlen - z->outpos) return -4;
            {
                unsigned char *p = z->out + z->outpos;
                const unsigned char *q = p - d;
                size_t n = len;
                while (n--) *p++ = *q++; /* may overlap: byte by byte */
            }
            z->outpos += len;
        }
        /* No per-symbol input check: every symbol writes output, which is
         * bounded by outlen, and the end of the block checks the input. */
    }
}

static int pyx_dynamic(pyx_inflater *z) {
    static const unsigned char order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5,
                                            11, 4, 12, 3, 13, 2, 14, 1, 15};
    unsigned char lengths[286 + 30], cl[19];
    pyx_huffman clh;
    int hlit, hdist, hclen, i, n = 0;
    hlit = (int)pyx_take(z, 5) + 257;
    hdist = (int)pyx_take(z, 5) + 1;
    hclen = (int)pyx_take(z, 4) + 4;
    if (hlit > 286 || hdist > 30) return -7;
    memset(cl, 0, sizeof(cl));
    for (i = 0; i < hclen; i++) cl[order[i]] = (unsigned char)pyx_take(z, 3);
    if (!pyx_build(&clh, cl, 19)) return -7;
    while (n < hlit + hdist) {
        int c = pyx_decode(z, &clh), rep;
        unsigned char fill = 0;
        if (c < 0 || c >= 19) return -7;
        if (c < 16) {
            lengths[n++] = (unsigned char)c;
            continue;
        }
        if (c == 16) {
            if (n == 0) return -7;
            fill = lengths[n - 1];
            rep = 3 + (int)pyx_take(z, 2);
        } else if (c == 17) {
            rep = 3 + (int)pyx_take(z, 3);
        } else {
            rep = 11 + (int)pyx_take(z, 7);
        }
        if (n + rep > hlit + hdist) return -7;
        while (rep--) lengths[n++] = fill;
        if (pyx_overrun(z)) return -5;
    }
    if (lengths[256] == 0) return -7; /* no end-of-block code */
    if (!pyx_build(&z->lit, lengths, hlit)) return -7;
    if (!pyx_build(&z->dist, lengths + hlit, hdist)) return -7;
    return pyx_codes(z);
}

static int pyx_fixed(pyx_inflater *z) {
    unsigned char lengths[288];
    int i;
    for (i = 0; i < 144; i++) lengths[i] = 8;
    for (; i < 256; i++) lengths[i] = 9;
    for (; i < 280; i++) lengths[i] = 7;
    for (; i < 288; i++) lengths[i] = 8;
    if (!pyx_build(&z->lit, lengths, 288)) return -7;
    for (i = 0; i < 30; i++) lengths[i] = 5;
    if (!pyx_build(&z->dist, lengths, 30)) return -7;
    return pyx_codes(z);
}

static int pyx_stored(pyx_inflater *z) {
    unsigned char hdr[4];
    size_t len;
    int k = 0;
    pyx_take(z, z->nbits & 7); /* to a byte boundary */
    while (z->nbits > 0 && k < 4) {
        hdr[k++] = (unsigned char)(z->bits & 255);
        z->bits >>= 8;
        z->nbits -= 8;
    }
    if (z->nbits > 0) {
        /* whole bytes still buffered after the header: give them back */
        z->inpos -= (size_t)(z->nbits / 8);
        z->bits = 0;
        z->nbits = 0;
    }
    while (k < 4) {
        if (z->inpos >= z->inlen) return -5;
        hdr[k++] = z->in[z->inpos++];
    }
    len = (size_t)hdr[0] | ((size_t)hdr[1] << 8);
    if ((hdr[2] | (hdr[3] << 8)) != (int)(~len & 0xFFFF)) return -8;
    if (z->inpos > z->inlen || len > z->inlen - z->inpos) return -5;
    if (len > z->outlen - z->outpos) return -4;
    memcpy(z->out + z->outpos, z->in + z->inpos, len);
    z->inpos += len;
    z->outpos += len;
    return 0;
}

/*
 * Decode the raw DEFLATE stream src[0:srclen] into dst[0:dstlen].
 * Returns the number of bytes written, or a negative error code.
 */
static long long pyx_inflate(pyx_inflater *z, const unsigned char *src,
                             size_t srclen, unsigned char *dst, size_t dstlen) {
    int last, type, rc;
    z->in = src;
    z->inlen = srclen;
    z->inpos = 0;
    z->bits = 0;
    z->nbits = 0;
    z->out = dst;
    z->outlen = dstlen;
    z->outpos = 0;
    do {
        last = (int)pyx_take(z, 1);
        type = (int)pyx_take(z, 2);
        if (type == 0)
            rc = pyx_stored(z);
        else if (type == 1)
            rc = pyx_fixed(z);
        else if (type == 2)
            rc = pyx_dynamic(z);
        else
            rc = -2;
        if (rc < 0) return rc;
        if (pyx_overrun(z)) return -5;
    } while (!last);
    return (long long)z->outpos;
}

#endif
