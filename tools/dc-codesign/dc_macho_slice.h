/* Bounded, allocation-free architecture selection shared by mldr's gate and loader. */
#ifndef DC_MACHO_SLICE_H
#define DC_MACHO_SLICE_H
#include <stdint.h>
#include <stddef.h>

#define DC_CPU_X86 7u
#define DC_CPU_X86_64 0x01000007u
#define DC_MAX_FAT_ARCHES 64u

struct dc_macho_slice { uint64_t offset, size; uint32_t cpu, subtype; };

static uint32_t dc_slice_u32(const uint8_t *p, int big) {
    if (big) return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
    return ((uint32_t)p[3] << 24) | ((uint32_t)p[2] << 16) | ((uint32_t)p[1] << 8) | p[0];
}
static uint64_t dc_slice_u64(const uint8_t *p, int big) {
    if (big) return ((uint64_t)dc_slice_u32(p, 1) << 32) | dc_slice_u32(p + 4, 1);
    return ((uint64_t)dc_slice_u32(p + 4, 0) << 32) | dc_slice_u32(p, 0);
}
static int dc_slice_header(const uint8_t *p, uint64_t size, uint32_t *cpu, uint32_t *subtype) {
    if (size < 28) return -1;
    uint32_t magic = dc_slice_u32(p, 1);
    int big, wide = 0;
    switch (magic) {
    case 0xfeedfaceu: big = 1; break;
    case 0xcefaedfeu: big = 0; break;
    case 0xfeedfacfu: big = 1; wide = 1; if (size < 32) return -1; break;
    case 0xcffaedfeu: big = 0; wide = 1; if (size < 32) return -1; break;
    default: return -1;
    }
    *cpu = dc_slice_u32(p + 4, big);
    *subtype = dc_slice_u32(p + 8, big);
    if (wide != ((*cpu & 0x01000000u) != 0)) return -1;
    return 0;
}

/* Returns -1 for unsupported or malformed input. Does not validate signatures.
 * host_cpu is the loader ABI; x86_64 may select x86 for re-execution in mldr32.
 * Preferences retain the existing first-matching-preference behavior; otherwise
 * prefer the native ABI, falling back to x86 only on an x86_64 host.
 * Output is untouched on error. */
static int dc_select_macho_slice(const uint8_t *data, size_t size,
    const uint32_t preferences[4], uint32_t forced_cpu, uint32_t host_cpu,
    struct dc_macho_slice *out) {
    struct dc_macho_slice slices[DC_MAX_FAT_ARCHES];
    unsigned count = 1;
    if (!data || !out || size < 4 ||
        (host_cpu != DC_CPU_X86 && host_cpu != DC_CPU_X86_64)) return -1;
    uint32_t magic = dc_slice_u32(data, 1);
    int fat = magic == 0xcafebabeu || magic == 0xbebafecau ||
              magic == 0xcafebabfu || magic == 0xbfbafecau;
    if (fat) {
        if (size < 8) return -1;
        int big = magic == 0xcafebabeu || magic == 0xcafebabfu;
        int wide = magic == 0xcafebabfu || magic == 0xbfbafecau;
        unsigned width = wide ? 32 : 20;
        count = dc_slice_u32(data + 4, big);
        if (!count || count > DC_MAX_FAT_ARCHES || count > (size - 8) / width) return -1;
        uint64_t table_end = 8 + (uint64_t)count * width;
        for (unsigned i = 0; i < count; ++i) {
            const uint8_t *row = data + 8 + (size_t)i * width;
            slices[i].cpu = dc_slice_u32(row, big);
            slices[i].subtype = dc_slice_u32(row + 4, big);
            slices[i].offset = wide ? dc_slice_u64(row + 8, big) : dc_slice_u32(row + 8, big);
            slices[i].size = wide ? dc_slice_u64(row + 16, big) : dc_slice_u32(row + 12, big);
            uint32_t align = dc_slice_u32(row + (wide ? 24 : 16), big);
            if (align >= 63 || slices[i].offset < table_end || slices[i].offset > size ||
                !slices[i].size || slices[i].size > size - slices[i].offset ||
                (slices[i].offset & ((UINT64_C(1) << align) - 1))) return -1;
            uint32_t cpu, subtype;
            if (dc_slice_header(data + (size_t)slices[i].offset, slices[i].size, &cpu, &subtype) ||
                cpu != slices[i].cpu || subtype != slices[i].subtype) return -1;
            for (unsigned j = 0; j < i; ++j) {
                if (slices[i].offset < slices[j].offset + slices[j].size &&
                    slices[j].offset < slices[i].offset + slices[i].size) return -1;
                if (slices[i].cpu == slices[j].cpu && slices[i].subtype == slices[j].subtype) return -1;
            }
        }
    } else {
        slices[0].offset = 0; slices[0].size = size;
        if (dc_slice_header(data, size, &slices[0].cpu, &slices[0].subtype)) return -1;
    }
    int best = -1;
    unsigned best_preference = 4;
    for (unsigned i = 0; i < count; ++i) {
        uint32_t cpu = slices[i].cpu;
        if (cpu != host_cpu && !(host_cpu == DC_CPU_X86_64 && cpu == DC_CPU_X86)) continue;
        if (forced_cpu) {
            if (cpu == forced_cpu) best = (int)i;
            continue;
        }
        unsigned preference = 4;
        if (preferences) for (unsigned j = 0; j < 4; ++j) {
            if (preferences[j] && preferences[j] == cpu) { preference = j; break; }
        }
        if (preference < best_preference) { best = (int)i; best_preference = preference; }
        else if (best_preference == 4 && (cpu == host_cpu || best < 0)) best = (int)i;
    }
    if (best < 0) return -1;
    *out = slices[best];
    return 0;
}
#endif
