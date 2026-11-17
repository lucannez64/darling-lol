// Iridium's output types and an inert translator; the on-disk translation
// cache is extracted from the authoritative component patch, not copied here.
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace Iridium {
    enum class FunctionType { Vertex = 1, Fragment = 2, Kernel = 3 };
    enum class BindingType { Buffer, Texture, Sampler, VertexInput };
    enum class TextureAccessType { Sample, Read, Write, ReadWrite };
    struct BindingInfo {
        BindingType type;
        size_t index;
        size_t internalIndex;
        TextureAccessType textureAccessType;
        size_t embeddedSamplerIndex;
    };
    struct EmbeddedSampler { uint8_t modes[10]; float lodMin; float lodMax; };
    struct StructMemberInfo { size_t offset; std::string typeName; std::string name; };
    struct ArgumentInfo {
        BindingType type;
        size_t index = 0;
        std::string name;
        std::string typeName;
        size_t size = 0;
        size_t alignment = 0;
        std::vector<StructMemberInfo> members;
    };
    struct FunctionInfo {
        FunctionType type;
        std::vector<BindingInfo> bindings;
        std::vector<ArgumentInfo> arguments;
        std::vector<EmbeddedSampler> embeddedSamplers;
    };
    struct OutputInfo { std::unordered_map<std::string, FunctionInfo> functionInfos; };
}
namespace Indium { class PrivateDevice; class PrivateLibrary; }

namespace {
/* ACTUAL_PATCHED_CACHE */
}

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #condition); exit(1); } } while (0)

static const char input[] = "not really AIR, only the bytes the cache key is made from";
static unsigned translations = 0;

// Each run of the translator is recognisable, so a stale entry cannot pass for a fresh translation.
static bool translate(CachedTranslatedLibrary& fresh) {
    ++translations;
    fresh.spirv.resize(64);
    for (size_t word = 0; word < 16; ++word) memcpy(fresh.spirv.data() + word * 4, &translations, 4);
    auto& function = fresh.outputInfo.functionInfos["main0"];
    function.type = Iridium::FunctionType::Fragment;
    Iridium::BindingInfo binding {};
    binding.type = Iridium::BindingType::Texture;
    binding.index = 3;
    binding.internalIndex = translations;
    function.bindings.push_back(binding);
    Iridium::EmbeddedSampler sampler {};
    sampler.lodMax = 4.0f;
    function.embeddedSamplers.push_back(sampler);
    Iridium::ArgumentInfo argument;
    argument.type = Iridium::BindingType::Buffer;
    argument.index = 1;
    argument.name = "uniforms";
    argument.typeName = "Uniforms";
    argument.size = 32;
    argument.alignment = 16;
    argument.members.push_back({8, "float4", "tint"});
    function.arguments.push_back(argument);
    return true;
}

// The translation a caller gets back: its generation, or 0 when it is not one complete translation.
static unsigned generation(const CachedTranslatedLibrary& entry) {
    if (entry.spirv.size() != 64 || entry.outputInfo.functionInfos.size() != 1) return 0;
    const auto found = entry.outputInfo.functionInfos.find("main0");
    if (found == entry.outputInfo.functionInfos.end()) return 0;
    const auto& function = found->second;
    if (function.bindings.size() != 1 || function.embeddedSamplers.size() != 1 || function.arguments.size() != 1) return 0;
    if (function.arguments[0].name != "uniforms" || function.arguments[0].members.size() != 1) return 0;
    if (function.arguments[0].members[0].name != "tint" || function.embeddedSamplers[0].lodMax != 4.0f) return 0;
    unsigned stamp = 0;
    memcpy(&stamp, entry.spirv.data(), 4);
    for (size_t word = 0; word < 16; ++word) if (memcmp(entry.spirv.data() + word * 4, &stamp, 4) != 0) return 0;
    return function.bindings[0].internalIndex == stamp ? stamp : 0;
}

static unsigned request(const std::string& dir, const std::string& identity) {
    CachedTranslatedLibrary entry;
    CHECK(loadOrTranslateLibrary(dir, identity, computeLibraryCacheKey(input, sizeof(input)), entry, translate));
    const unsigned result = generation(entry);
    CHECK(result != 0);
    return result;
}

static std::vector<char> readFile(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    return std::vector<char>(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

static void writeFile(const std::string& path, const std::vector<char>& bytes) {
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    file.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
}

static bool exists(const std::string& path) { return access(path.c_str(), F_OK) == 0; }

int main(int argc, char** argv) {
    CHECK(argc == 2);
    const std::string dir = argv[1];
    const std::string x = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-llvm22.1.0";
    const std::string sameSourcesNewerLLVM = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-llvm22.1.1";
    const std::string otherSources = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-llvm22.1.0";
    const auto key = computeLibraryCacheKey(input, sizeof(input));
    const std::string xPath = iridiumCacheFilePath(dir, x, key);

    // The same translator translates once and then reads its own entry.
    CHECK(request(dir, x) == 1);
    CHECK(exists(xPath));
    CHECK(request(dir, x) == 1);
    CHECK(translations == 1);

    // Another translator, by its sources or by its LLVM, never gets that entry.
    CHECK(iridiumCacheFilePath(dir, sameSourcesNewerLLVM, key) != xPath);
    CHECK(iridiumCacheFilePath(dir, otherSources, key) != xPath);
    CHECK(request(dir, sameSourcesNewerLLVM) == 2);
    CHECK(request(dir, otherSources) == 3);
    CHECK(request(dir, sameSourcesNewerLLVM) == 2);
    CHECK(request(dir, x) == 1);
    CHECK(translations == 3);

    // Even under its own file name, an entry from another translator is rejected by its header.
    const auto xBytes = readFile(xPath);
    const std::string otherPath = iridiumCacheFilePath(dir, otherSources, key);
    writeFile(otherPath, xBytes);
    CHECK(request(dir, otherSources) == 4);
    CHECK(request(dir, otherSources) == 4);

    // A truncated entry is translated again and replaced, at every length.
    unsigned expected = 4;
    for (size_t length = 0; length < xBytes.size(); ++length) {
        writeFile(xPath, std::vector<char>(xBytes.begin(), xBytes.begin() + static_cast<std::ptrdiff_t>(length)));
        CHECK(request(dir, x) == ++expected);
        CHECK(readFile(xPath).size() == xBytes.size());
    }
    CHECK(request(dir, x) == expected);

    // So is an entry with one damaged byte anywhere, or with bytes appended.
    const auto valid = readFile(xPath);
    for (size_t offset = 0; offset < valid.size(); ++offset) {
        auto damaged = valid;
        damaged[offset] = static_cast<char>(damaged[offset] ^ 0x40);
        writeFile(xPath, damaged);
        CHECK(request(dir, x) == ++expected);
    }
    auto longer = readFile(xPath);
    longer.push_back(0);
    writeFile(xPath, longer);
    CHECK(request(dir, x) == ++expected);
    CHECK(request(dir, x) == expected);
    CHECK(translations == expected);

    // Without a translator identity or a directory nothing is stored or read.
    const std::string empty = dir + "/empty";
    CHECK(mkdir(empty.c_str(), 0755) == 0);
    CHECK(request(empty, "") == ++expected);
    CHECK(request(empty, "") == ++expected);
    CHECK(request("", x) == ++expected);
    DIR* handle = opendir(empty.c_str());
    CHECK(handle);
    while (const dirent* item = readdir(handle)) CHECK(item->d_name[0] == '.');
    closedir(handle);

    // Entries of other translators, the old constant-version ones included, are deleted; nothing else is.
    const std::string oldEntry = dir + "/iridium-v3-0123456789abcdef-0123456789abcdef-40.bin";
    const std::string pipelineCache = dir + "/vkPipelineCache-1002-73bf.bin";
    writeFile(oldEntry, valid);
    writeFile(pipelineCache, valid);
    const std::string newerPath = iridiumCacheFilePath(dir, sameSourcesNewerLLVM, key);
    CHECK(exists(newerPath) && exists(otherPath));
    removeForeignTranslations(dir, x);
    CHECK(exists(xPath) && exists(pipelineCache) && exists(empty));
    CHECK(!exists(oldEntry) && !exists(newerPath) && !exists(otherPath));
    CHECK(request(dir, x) == expected - 3);

    puts("PASS: translation cache");
    return 0;
}
