#include "dc_macho_slice.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void word(uint8_t *p, uint32_t x, int big) {
    for (unsigned i = 0; i < 4; ++i) p[big ? 3 - i : i] = (uint8_t)(x >> (8 * i));
}
static void thin(uint8_t *p, uint32_t cpu) {
    memset(p, 0, 32); word(p, cpu & 0x01000000u ? 0xfeedfacf : 0xfeedface, 0);
    word(p + 4, cpu, 0); word(p + 8, 3, 0);
}
static void fixture(uint8_t *p, int big, int wide) {
    memset(p, 0, 256);
    word(p, wide ? 0xcafebabf : 0xcafebabe, big); word(p + 4, 2, big);
    unsigned width = wide ? 32 : 20;
    for (unsigned i = 0; i < 2; ++i) {
        uint8_t *row = p + 8 + i * width;
        word(row, i ? DC_CPU_X86_64 : DC_CPU_X86, big); word(row + 4, 3, big);
        if (wide) {
            word(row + (big ? 12 : 8), 128 + 64 * i, big);
            word(row + (big ? 20 : 16), 32, big); word(row + 24, 6, big);
        } else {
            word(row + 8, 128 + 64 * i, big); word(row + 12, 32, big); word(row + 16, 6, big);
        }
        thin(p + 128 + 64 * i, i ? DC_CPU_X86_64 : DC_CPU_X86);
    }
}
int main(void) {
    uint8_t data[256]; struct dc_macho_slice s;
    uint32_t preferences[4] = {DC_CPU_X86, DC_CPU_X86_64, 0, 0};
    for (int big = 0; big < 2; ++big) for (int wide = 0; wide < 2; ++wide) {
        fixture(data, big, wide);
        assert(!dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s));
        assert(s.cpu == DC_CPU_X86_64 && s.offset == 192 && s.size == 32);
        assert(!dc_select_macho_slice(data, sizeof(data), preferences, 0, DC_CPU_X86_64, &s));
        assert(s.cpu == DC_CPU_X86 && s.offset == 128);
        assert(!dc_select_macho_slice(data, sizeof(data), preferences, DC_CPU_X86_64, DC_CPU_X86_64, &s));
        assert(s.cpu == DC_CPU_X86_64);
        assert(!dc_select_macho_slice(data, sizeof(data), preferences, 0, DC_CPU_X86, &s));
        assert(s.cpu == DC_CPU_X86);
        assert(dc_select_macho_slice(data, 40, NULL, 0, DC_CPU_X86_64, &s) == -1);
    }
    thin(data, DC_CPU_X86_64);
    assert(!dc_select_macho_slice(data, 32, NULL, 0, DC_CPU_X86_64, &s) && !s.offset);
    assert(dc_select_macho_slice(data, 32, NULL, DC_CPU_X86, DC_CPU_X86_64, &s) == -1);
    fixture(data, 1, 0);
    word(data + 8 + 20 + 8, 128, 1); /* overlapping slices */
    assert(dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s) == -1);
    fixture(data, 1, 0);
    word(data + 8 + 12, 0xffffffffu, 1); /* out-of-file size */
    assert(dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s) == -1);
    fixture(data, 1, 0);
    word(data + 192 + 4, DC_CPU_X86, 0); /* table/header disagreement */
    assert(dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s) == -1);
    fixture(data, 1, 0);
    word(data + 8 + 16, 64, 1); /* invalid alignment exponent */
    assert(dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s) == -1);
    fixture(data, 1, 1);
    word(data + 8 + 8, 0xffffffffu, 1); /* 64-bit offset cannot wrap into bounds */
    assert(dc_select_macho_slice(data, sizeof(data), NULL, 0, DC_CPU_X86_64, &s) == -1);
    struct dc_macho_slice before = s;
    assert(dc_select_macho_slice(data, 3, NULL, 0, DC_CPU_X86_64, &s) == -1);
    assert(!memcmp(&s, &before, sizeof(s)));
    puts("PASS native/preferred/forced architecture, fat32/fat64 endianness, malformed bounds and unchanged error output");
}
