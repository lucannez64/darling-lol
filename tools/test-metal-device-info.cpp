// Host check for the GPU name and working-set helpers behind -[MTLDevice name] and
// -[MTLDevice recommendedMaxWorkingSetSize] (private-include/Metal/MTLDeviceInfo.h).
// Build and run after preparing the sources (see docs/TESTING.md):
//   c++ -std=c++17 -I build/darling-src/src/external/metal/private-include
//       tools/test-metal-device-info.cpp -o build/test-metal-device-info
//   build/test-metal-device-info
#include <Metal/MTLDeviceInfo.h>
#include <cstdio>
#include <string>

static int failures = 0;

static void expectName(const char* vulkan, const char* expected) {
	std::string actual = MTLDeviceInfo::metalNameFromVulkanName(vulkan);
	bool ok = actual == expected;
	failures += !ok;
	std::printf("%s name \"%s\" -> \"%s\"%s%s%s\n", ok ? "PASS" : "FAIL", vulkan ? vulkan : "(null)",
	            actual.c_str(), ok ? "" : " (expected \"", ok ? "" : expected, ok ? "" : "\")");
}

static void expectBytes(const char* what, unsigned long long actual, unsigned long long expected) {
	bool ok = actual == expected;
	failures += !ok;
	std::printf("%s %s -> %llu%s\n", ok ? "PASS" : "FAIL", what, actual, ok ? "" : " (mismatch)");
	if (!ok) std::printf("     expected %llu\n", expected);
}

int main() {
	// Mesa and proprietary Vulkan names -> macOS Metal style.
	expectName("AMD Radeon RX 6800 XT (RADV NAVI21)", "AMD Radeon RX 6800 XT");
	expectName("AMD Radeon RX 6650 XT (RADV NAVI23)", "AMD Radeon RX 6650 XT");
	expectName("AMD Radeon Graphics (RADV RENOIR)", "AMD Radeon Graphics");
	expectName("AMD Radeon Pro 5500M (RADV NAVI14)", "AMD Radeon Pro 5500M");
	expectName("AMD RADV NAVI21 (ACO)", "AMD RADV NAVI21");
	expectName("Intel(R) UHD Graphics 630 (CML GT2)", "Intel UHD Graphics 630");
	expectName("Intel(R) Arc(TM) A770 Graphics (DG2)", "Intel Arc A770 Graphics");
	expectName("NVIDIA GeForce GTX 680", "NVIDIA GeForce GTX 680");
	expectName("NVIDIA GeForce RTX 2070 SUPER", "NVIDIA GeForce RTX 2070 SUPER");
	expectName("NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 4090");
	expectName("llvmpipe (LLVM 22.1.0, 256 bits)", "llvmpipe");
	// Whitespace, nesting and edge cases.
	expectName("  AMD   Radeon\tRX 7900 XTX  (RADV NAVI31) ", "AMD Radeon RX 7900 XTX");
	expectName("Foo GPU (driver (nested) x)", "Foo GPU");
	expectName("Foo (Bar) GPU", "Foo (Bar) GPU");     // not trailing: kept
	expectName("(RADV NAVI21)", "(RADV NAVI21)");     // whole name: kept
	expectName("Broken (unbalanced", "Broken (unbalanced");
	expectName("(R)(TM)", "(R)(TM)");                 // cleaned empty -> raw name
	expectName("   ", MTLDeviceInfo::kFallbackName);  // raw empty -> neutral fallback
	expectName("", MTLDeviceInfo::kFallbackName);
	expectName(nullptr, MTLDeviceInfo::kFallbackName);

	// -[MTLDevice registryID]: never 0, like a real Mac.
	{
		bool ok = MTLDeviceInfo::kDefaultRegistryID != 0
		       && MTLDeviceInfo::registryIdFor(0, 0) != 0;
		failures += !ok;
		std::printf("%s registry ID is non-zero\n", ok ? "PASS" : "FAIL");
	}

	// Per-card registry IDs: distinct for the GPUs of a hybrid host, stable per card.
	{
		using MTLDeviceInfo::registryIdFor;
		bool ok = registryIdFor(0x1002, 0x73ff) == registryIdFor(0x1002, 0x73ff)
		       && registryIdFor(0x1002, 0x73ff) != registryIdFor(0x10de, 0x2503)
		       && registryIdFor(0x8086, 0x9a49) != registryIdFor(0x10de, 0x2520)
		       && registryIdFor(0x1002, 0x164e) != registryIdFor(0x1002, 0x744c)
		       && registryIdFor(0x1002, 0x73ff) >= MTLDeviceInfo::kRegistryIdBase
		       && registryIdFor(0x1002, 0x73ff) < MTLDeviceInfo::kRegistryIdBase + MTLDeviceInfo::kRegistryIdSlots;
		failures += !ok;
		std::printf("%s per-card registry ID\n", ok ? "PASS" : "FAIL");
	}

	// DARLING_METAL_GPU_NAME override.
	{
		bool ok = MTLDeviceInfo::resolveName("My GPU", "AMD Radeon RX 6800 XT (RADV NAVI21)") == "My GPU"
		       && MTLDeviceInfo::resolveName("", "AMD Radeon RX 6800 XT (RADV NAVI21)") == "AMD Radeon RX 6800 XT"
		       && MTLDeviceInfo::resolveName(nullptr, "NVIDIA GeForce RTX 4090") == "NVIDIA GeForce RTX 4090";
		failures += !ok;
		std::printf("%s name override\n", ok ? "PASS" : "FAIL");
	}

	// DARLING_METAL_MAC_GPU_NAME: MacBookPro16,1 GPUs; AMD and NVIDIA hosts present its
	// Radeon Pro 5500M, Intel its UHD Graphics 630; software devices keep their name.
	{
		using MTLDeviceInfo::macGpuName;
		bool ok = std::string(macGpuName(0x1002)) == "AMD Radeon Pro 5500M"
		       && std::string(macGpuName(0x8086)) == "Intel UHD Graphics 630"
		       && std::string(macGpuName(0x10de)) == "AMD Radeon Pro 5500M"
		       && macGpuName(0x10005) == nullptr
		       && MTLDeviceInfo::resolveName(nullptr, "AMD Radeon RX 6600 (RADV NAVI23)", macGpuName(0x1002)) == "AMD Radeon Pro 5500M"
		       && MTLDeviceInfo::resolveName(nullptr, "NVIDIA GeForce GTX 1060 6GB", macGpuName(0x10de)) == "AMD Radeon Pro 5500M"
		       && MTLDeviceInfo::resolveName(nullptr, "llvmpipe (LLVM 22.1.0, 256 bits)", macGpuName(0x10005)) == "llvmpipe"
		       && MTLDeviceInfo::resolveName("My GPU", "AMD Radeon RX 6600 (RADV NAVI23)", macGpuName(0x1002)) == "My GPU";
		failures += !ok;
		std::printf("%s Mac GPU name\n", ok ? "PASS" : "FAIL");
	}

	// DARLING_METAL_VRAM_MB parsing: positive decimal integer only.
	const unsigned long long MiB = 1024ull * 1024ull;
	expectBytes("vram \"8192\"", MTLDeviceInfo::parseMegabytes("8192"), 8192 * MiB);
	expectBytes("vram \"1\"", MTLDeviceInfo::parseMegabytes("1"), MiB);
	expectBytes("vram \"0\"", MTLDeviceInfo::parseMegabytes("0"), 0);
	expectBytes("vram \"\"", MTLDeviceInfo::parseMegabytes(""), 0);
	expectBytes("vram null", MTLDeviceInfo::parseMegabytes(nullptr), 0);
	expectBytes("vram \"-5\"", MTLDeviceInfo::parseMegabytes("-5"), 0);
	expectBytes("vram \"8G\"", MTLDeviceInfo::parseMegabytes("8G"), 0);
	expectBytes("vram \" 8\"", MTLDeviceInfo::parseMegabytes(" 8"), 0);
	expectBytes("vram \"1.5\"", MTLDeviceInfo::parseMegabytes("1.5"), 0);
	expectBytes("vram max", MTLDeviceInfo::parseMegabytes("17592186044415"), 17592186044415ull * MiB);
	expectBytes("vram overflow", MTLDeviceInfo::parseMegabytes("17592186044416"), 0);
	expectBytes("vram huge", MTLDeviceInfo::parseMegabytes("99999999999999999999999"), 0);

	// Working set from Vulkan heaps.
	const MTLDeviceInfo::Heap discrete[] = {{8192 * MiB, true}, {16384 * MiB, false}, {256 * MiB, true}};
	expectBytes("discrete heaps", MTLDeviceInfo::workingSetBytes(discrete, 3, false), 8448 * MiB);
	const MTLDeviceInfo::Heap apu[] = {{512 * MiB, true}, {15000 * MiB, false}};
	expectBytes("integrated heaps", MTLDeviceInfo::workingSetBytes(apu, 2, true), 15512 * MiB);
	const MTLDeviceInfo::Heap hostOnly[] = {{4096 * MiB, false}};
	expectBytes("discrete without device-local heap", MTLDeviceInfo::workingSetBytes(hostOnly, 1, false), 0);
	expectBytes("no heaps", MTLDeviceInfo::workingSetBytes(nullptr, 0, false), 0);
	const MTLDeviceInfo::Heap huge[] = {{UINT64_MAX, true}, {UINT64_MAX, true}};
	expectBytes("saturating sum", MTLDeviceInfo::workingSetBytes(huge, 2, false), UINT64_MAX);

	std::printf("METAL_DEVICE_INFO failures=%d pass=%d\n", failures, failures == 0);
	return failures == 0 ? 0 : 1;
}
