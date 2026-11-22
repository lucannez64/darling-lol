"""Host-only tests for CoreAudio AudioHardwareImpl volume and mute properties."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches' / 'darling.patch'


def patch_section(path):
    marker = f'diff --git a/{path} b/{path}\n'
    text = PATCH.read_text()
    parts = text.split(marker)[1:]
    if not parts:
        raise AssertionError(f'{path} not found in {PATCH}')
    return '\n'.join(part.split('\ndiff --git ', 1)[0] for part in parts)


def added_lines(section):
    return '\n'.join(
        line[1:]
        for line in section.splitlines()
        if line.startswith('+') and not line.startswith('+++')
    )


def postimage(section):
    return '\n'.join(
        line[1:]
        for line in section.splitlines()
        if (line.startswith(' ') or (line.startswith('+') and not line.startswith('+++')))
    )


def extract_between(text, start, end):
    begin = text.index(start)
    stop = text.index(end, begin)
    return text[begin:stop]


class CoreAudioVolumeMuteTests(unittest.TestCase):
    def test_header_and_member_declarations(self):
        header = added_lines(patch_section('src/CoreAudio/include/CoreAudio/AudioHardware.h'))
        self.assertIn("kAudioDevicePropertyVolumeRangeDecibels = 'vdb#',", header)

        impl_h = added_lines(patch_section('src/CoreAudio/CoreAudio/AudioHardwareImpl.h'))
        self.assertIn('#include <atomic>', impl_h)
        self.assertIn('std::atomic<Float32> m_volumeScalar{1.0f};', impl_h)
        self.assertIn('std::atomic<UInt32> m_muted{0};', impl_h)

    def test_volume_and_mute_properties_runtime(self):
        compiler = shutil.which('c++')
        if compiler is None:
            self.skipTest('needs a C++17 compiler')

        impl_cpp = postimage(patch_section('src/CoreAudio/CoreAudio/AudioHardwareImpl.cpp'))
        helpers = extract_between(
            impl_cpp,
            'static constexpr Float32 kVolumeMinDecibels',
            'AudioHardwareImpl::AudioHardwareImpl',
        )

        # Pull the exact volume/mute switch cases from isPropertySettable,
        # getPropertyData and setPropertyData in the patched postimage.
        settable_cases = extract_between(
            impl_cpp,
            '\t\tcase kAudioDevicePropertyVolumeScalar:\n\t\tcase kAudioDevicePropertyVolumeDecibels:\n\t\tcase kAudioDevicePropertyMute:',
            '\t}\n\n\t*outIsSettable = false;',
        )
        get_start = impl_cpp.index(
            '\t\tcase kAudioDevicePropertyVolumeScalar:\n\t\t{\n'
            '\t\t\tif (m_myId == kAudioObjectSystemObject)\n'
            '\t\t\t\treturn kAudioHardwareUnknownPropertyError;\n'
            '\t\t\tif (outData'
        )
        get_end = impl_cpp.index('\t}\nOSStatus AudioHardwareImpl::setPropertyData', get_start)
        get_cases = impl_cpp[get_start:get_end]

        set_start = impl_cpp.index(
            '\t\tcase kAudioDevicePropertyVolumeScalar:\n\t\t{\n'
            '\t\t\tif (m_myId == kAudioObjectSystemObject)\n'
            '\t\t\t\treturn kAudioHardwareUnknownPropertyError;\n'
            '\t\t\tif (!inData'
        )
        set_end = impl_cpp.index('\t\tcase kAudioDevicePropertyStreamFormat:', set_start)
        set_cases = impl_cpp[set_start:set_end]

        program = f'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <limits>

using UInt32 = uint32_t;
using Float32 = float;
using Float64 = double;
using Boolean = bool;
using OSStatus = int32_t;
using AudioObjectID = UInt32;

struct AudioValueRange {{
    Float64 mMinimum;
    Float64 mMaximum;
}};

struct AudioObjectPropertyAddress {{
    UInt32 mSelector;
    UInt32 mScope;
    UInt32 mElement;
}};

enum {{
    noErr = 0,
    paramErr = -50,
    kAudioObjectSystemObject = 1,
    kAudioHardwareNoError = 0,
    kAudioHardwareUnknownPropertyError = 'who?',
    kAudioHardwareBadPropertySizeError = '!siz',
    kAudioDevicePropertyVolumeScalar = 'volm',
    kAudioDevicePropertyVolumeDecibels = 'vold',
    kAudioDevicePropertyVolumeRangeDecibels = 'vdb#',
    kAudioDevicePropertyMute = 'mute',
}};

{helpers}

struct TestDevice {{
    AudioObjectID m_myId;
    std::atomic<Float32> m_volumeScalar{{1.0f}};
    std::atomic<UInt32> m_muted{{0}};
    std::atomic<UInt32> m_volumeGeneration{{0}};

    explicit TestDevice(AudioObjectID id) : m_myId(id) {{}}

    OSStatus isPropertySettable(const AudioObjectPropertyAddress* inAddress, Boolean* outIsSettable) {{
        switch (inAddress->mSelector) {{
{settable_cases}
        }}
        *outIsSettable = false;
        return kAudioHardwareUnknownPropertyError;
    }}

    OSStatus getPropertyData(const AudioObjectPropertyAddress* inAddress, UInt32* ioDataSize, void* outData) {{
        switch (inAddress->mSelector) {{
{get_cases}
        }}
        return kAudioHardwareUnknownPropertyError;
    }}

    OSStatus setPropertyData(const AudioObjectPropertyAddress* inAddress, UInt32 inDataSize, const void* inData) {{
        switch (inAddress->mSelector) {{
{set_cases}
        }}
        return kAudioHardwareUnknownPropertyError;
    }}
}};

int main() {{
    const AudioObjectPropertyAddress scalarAddr{{kAudioDevicePropertyVolumeScalar, 0, 0}};
    const AudioObjectPropertyAddress dbAddr{{kAudioDevicePropertyVolumeDecibels, 0, 0}};
    const AudioObjectPropertyAddress rangeAddr{{kAudioDevicePropertyVolumeRangeDecibels, 0, 0}};
    const AudioObjectPropertyAddress muteAddr{{kAudioDevicePropertyMute, 0, 0}};

    // System object must not expose device volume/mute properties.
    TestDevice sys(kAudioObjectSystemObject);
    for (const auto& addr : {{scalarAddr, dbAddr, rangeAddr, muteAddr}}) {{
        Boolean settable = true;
        assert(sys.isPropertySettable(&addr, &settable) == kAudioHardwareUnknownPropertyError);
        UInt32 size = 64;
        char buf[64] = {{}};
        assert(sys.getPropertyData(&addr, &size, buf) == kAudioHardwareUnknownPropertyError);
        assert(sys.setPropertyData(&addr, sizeof(Float32), buf) == kAudioHardwareUnknownPropertyError);
    }}

    // Regular device: defaults, sizes, settability.
    TestDevice dev(kAudioObjectSystemObject + 2);
    Boolean settable = false;
    assert(dev.isPropertySettable(&scalarAddr, &settable) == noErr && settable);
    assert(dev.isPropertySettable(&dbAddr, &settable) == noErr && settable);
    assert(dev.isPropertySettable(&muteAddr, &settable) == noErr && settable);
    assert(dev.isPropertySettable(&rangeAddr, &settable) == noErr && !settable);

    UInt32 size = 0;
    assert(dev.getPropertyData(&scalarAddr, &size, nullptr) == noErr && size == sizeof(Float32));
    size = 0;
    assert(dev.getPropertyData(&dbAddr, &size, nullptr) == noErr && size == sizeof(Float32));
    size = 0;
    assert(dev.getPropertyData(&rangeAddr, &size, nullptr) == noErr && size == sizeof(AudioValueRange));
    size = 0;
    assert(dev.getPropertyData(&muteAddr, &size, nullptr) == noErr && size == sizeof(UInt32));

    // Undersized output buffers fail cleanly.
    Float32 f = 123.0f;
    size = 2;
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == kAudioHardwareBadPropertySizeError);
    assert(dev.getPropertyData(&dbAddr, &size, &f) == kAudioHardwareBadPropertySizeError);
    AudioValueRange range{{1.0, 2.0}};
    size = sizeof(Float64);
    assert(dev.getPropertyData(&rangeAddr, &size, &range) == kAudioHardwareBadPropertySizeError);
    UInt32 m = 99;
    size = 2;
    assert(dev.getPropertyData(&muteAddr, &size, &m) == kAudioHardwareBadPropertySizeError);

    // Default values: full volume (1.0 scalar / 0.0 dB), range [-96, 0], unmuted.
    f = -1.0f; size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && f == 1.0f);
    f = -1.0f; size = sizeof(f);
    assert(dev.getPropertyData(&dbAddr, &size, &f) == noErr && f == 0.0f);
    size = sizeof(range);
    assert(dev.getPropertyData(&rangeAddr, &size, &range) == noErr && range.mMinimum == -96.0 && range.mMaximum == 0.0);
    m = 99; size = sizeof(m);
    assert(dev.getPropertyData(&muteAddr, &size, &m) == noErr && m == 0);

    // Reads never mark the volume as client-set.
    assert(dev.m_volumeGeneration.load() == 0);

    // Setting scalar persists and updates decibels.
    Float32 half = 0.5f;
    assert(dev.setPropertyData(&scalarAddr, sizeof(half), &half) == noErr);
    assert(dev.m_volumeGeneration.load() == 1);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && std::fabs(f - 0.5f) < 1e-6f);
    size = sizeof(f);
    assert(dev.getPropertyData(&dbAddr, &size, &f) == noErr && std::fabs(f - (-6.0206f)) < 1e-3f);

    // Setting decibels persists and updates scalar.
    Float32 minus20 = -20.0f;
    assert(dev.setPropertyData(&dbAddr, sizeof(minus20), &minus20) == noErr);
    assert(dev.m_volumeGeneration.load() == 2);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && std::fabs(f - 0.1f) < 1e-5f);
    size = sizeof(f);
    assert(dev.getPropertyData(&dbAddr, &size, &f) == noErr && std::fabs(f - (-20.0f)) < 1e-3f);

    // Clamping and NaN rejection; a rejected write leaves the generation alone.
    Float32 nan = std::numeric_limits<Float32>::quiet_NaN();
    assert(dev.setPropertyData(&scalarAddr, sizeof(nan), &nan) == paramErr);
    assert(dev.setPropertyData(&dbAddr, sizeof(nan), &nan) == paramErr);
    assert(dev.m_volumeGeneration.load() == 2);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && std::fabs(f - 0.1f) < 1e-5f);

    Float32 low = -2.0f, high = 3.0f;
    assert(dev.setPropertyData(&scalarAddr, sizeof(low), &low) == noErr);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && f == 0.0f);
    size = sizeof(f);
    assert(dev.getPropertyData(&dbAddr, &size, &f) == noErr && f == -96.0f);

    assert(dev.setPropertyData(&scalarAddr, sizeof(high), &high) == noErr);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && f == 1.0f);

    Float32 dbLow = -200.0f, dbHigh = 12.0f;
    assert(dev.setPropertyData(&dbAddr, sizeof(dbLow), &dbLow) == noErr);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && f == 0.0f);
    assert(dev.setPropertyData(&dbAddr, sizeof(dbHigh), &dbHigh) == noErr);
    size = sizeof(f);
    assert(dev.getPropertyData(&scalarAddr, &size, &f) == noErr && f == 1.0f);

    // Mute normalizes non-zero to 1 and persists.
    UInt32 muteOn = 42, muteOff = 0;
    const UInt32 beforeMute = dev.m_volumeGeneration.load();
    assert(dev.setPropertyData(&muteAddr, sizeof(muteOn), &muteOn) == noErr);
    assert(dev.m_volumeGeneration.load() == beforeMute + 1);
    size = sizeof(m);
    assert(dev.getPropertyData(&muteAddr, &size, &m) == noErr && m == 1);
    assert(dev.setPropertyData(&muteAddr, sizeof(muteOff), &muteOff) == noErr);
    size = sizeof(m);
    assert(dev.getPropertyData(&muteAddr, &size, &m) == noErr && m == 0);

    // Bad input sizes / null pointers fail cleanly.
    assert(dev.setPropertyData(&scalarAddr, 2, &half) == kAudioHardwareBadPropertySizeError);
    assert(dev.setPropertyData(&scalarAddr, sizeof(half), nullptr) == kAudioHardwareBadPropertySizeError);
    assert(dev.setPropertyData(&dbAddr, 2, &minus20) == kAudioHardwareBadPropertySizeError);
    assert(dev.setPropertyData(&muteAddr, 2, &muteOn) == kAudioHardwareBadPropertySizeError);

    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / 'volume_test.cpp'
            exe = Path(folder) / 'volume_test'
            src.write_text(program)
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-Wno-multichar', '-o', str(exe), str(src)],
                           check=True)
            subprocess.run([str(exe)], check=True)


class PulseInputVolumeTests(unittest.TestCase):
    """Issue #285: a client-set input volume must reach the PulseAudio record stream.

    Voice SDKs lower a Mac mic's input volume (automatic gain control, or the client's
    input slider). Darling stored the value and never applied it, so the mic stayed at
    full level. The stream now mirrors it onto its source output, but only once a client
    has set it.
    """

    STREAM = 'src/CoreAudio/CoreAudio/pulse/AudioHardwareStreamPAInput.cpp'

    def test_read_callback_applies_the_volume_before_delivering_audio(self):
        # The patch holds hunks, not the whole file; both calls sit in paStreamReadCB's hunks.
        stream = postimage(patch_section(self.STREAM))
        self.assertLess(stream.index('This->applyDeviceVolume(spec);'), stream.index('This->m_callback('))

    def test_apply_device_volume_runtime(self):
        compiler = shutil.which('c++')
        if compiler is None or not Path('/usr/include/pulse/volume.h').exists():
            self.skipTest('needs a C++17 compiler and the libpulse headers')
        stream = postimage(patch_section(self.STREAM))
        functions = extract_between(stream, 'static pa_volume_t paInputVolume',
                                    'AudioHardwareStreamPAInput::AudioHardwareStreamPAInput(')

        program = f'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <pulse/def.h>
#include <pulse/volume.h>

using UInt32 = uint32_t;
using Float32 = float;
typedef struct pa_stream pa_stream;
typedef struct pa_context pa_context;
typedef struct pa_operation pa_operation;

static uint32_t g_index = PA_INVALID_INDEX;
static int g_calls = 0;
static pa_cvolume g_last;
static pa_operation* g_op = reinterpret_cast<pa_operation*>(0x10);
static int g_unrefs = 0;

static uint32_t fake_index(pa_stream*) {{ return g_index; }}
static pa_context* fake_context(pa_stream*) {{ return reinterpret_cast<pa_context*>(0x20); }}
static pa_operation* fake_set(pa_context* c, uint32_t idx, const pa_cvolume* v, void*, void*) {{
    assert(c == reinterpret_cast<pa_context*>(0x20) && idx == g_index);
    g_last = *v; g_calls++; return g_op;
}}
static void fake_unref(pa_operation* op) {{ assert(op == g_op); g_unrefs++; }}
#define pa_stream_get_index fake_index
#define pa_stream_get_context fake_context
#define pa_context_set_source_output_volume fake_set
#define pa_operation_unref fake_unref
static bool rclolAudioTraceEnabled() {{ return false; }}
static void rclolAudioTrace(const char*, ...) {{}}

struct FakeHw {{
    UInt32 generation = 0; Float32 scalar = 1.0f; bool mute = false;
    UInt32 volumeGeneration() const {{ return generation; }}
    Float32 volumeScalar() const {{ return scalar; }}
    bool muted() const {{ return mute; }}
    UInt32 id() const {{ return 7; }}
}};

struct AudioHardwareStreamPAInput {{
    FakeHw* m_hw;
    pa_stream* m_stream = reinterpret_cast<pa_stream*>(0x30);
    UInt32 m_appliedVolumeGeneration = 0;
    void applyDeviceVolume(const pa_sample_spec* spec);
}};

{functions}

int main() {{
    FakeHw hw;
    AudioHardwareStreamPAInput s{{&hw}};
    pa_sample_spec spec{{PA_SAMPLE_FLOAT32LE, 44100, 2}};

    // Nobody set a volume: the user's mixer level is left alone.
    g_index = 3;
    s.applyDeviceVolume(&spec);
    assert(g_calls == 0);

    // A client lowers the volume before the stream has an index: retried later.
    hw.generation = 1; hw.scalar = 0.25f;
    g_index = PA_INVALID_INDEX;
    s.applyDeviceVolume(&spec);
    assert(g_calls == 0);

    g_index = 3;
    s.applyDeviceVolume(&spec);
    assert(g_calls == 1 && g_unrefs == 1);
    assert(g_last.channels == 2);
    assert(g_last.values[0] == pa_sw_volume_from_linear(0.25) && g_last.values[1] == g_last.values[0]);
    assert(g_last.values[0] < PA_VOLUME_NORM);

    // Applied once per change.
    s.applyDeviceVolume(&spec);
    assert(g_calls == 1);

    // Full scale is PA's 100 %, never an amplification.
    hw.generation = 2; hw.scalar = 1.0f;
    s.applyDeviceVolume(&spec);
    assert(g_calls == 2 && g_last.values[0] == PA_VOLUME_NORM);

    // Mute and a zero scalar silence the stream.
    hw.generation = 3; hw.mute = true;
    s.applyDeviceVolume(&spec);
    assert(g_last.values[0] == PA_VOLUME_MUTED);
    hw.generation = 4; hw.mute = false; hw.scalar = 0.0f;
    s.applyDeviceVolume(&spec);
    assert(g_calls == 4 && g_last.values[0] == PA_VOLUME_MUTED);
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / 'input_volume_test.cpp'
            exe = Path(folder) / 'input_volume_test'
            src.write_text(program)
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                            '-o', str(exe), str(src), '-lpulse'], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
