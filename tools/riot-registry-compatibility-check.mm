#import <Foundation/Foundation.h>
#include "IORegistryEntry.h"
#include "IOService.h"
#include "IOGraphicsAccelerator.h"
#include "IOGraphicsAcceleratorIds.h"
#include "PlatformIdentity.h"
#include <IOKit/IOReturn.h>
#include <IOKit/IOKitKeys.h>
#include <IOKit/IOCFUnserialize.h>
#include <mach/mach.h>
#include <cassert>
#include <cstring>
#include <cstdio>
#include <memory>
#include <vector>
#include <thread>
#include <dispatch/dispatch.h>
extern "C" {
#include "iokitmigServer.h"
#include "iokitmig.h"
}

// This probe invokes the real daemon objects and MIG handlers on the main
// thread, without registering a daemon or changing the running iokitd.
mach_port_t g_masterPort, g_deathPort, g_powerManagementPort;
struct Fixture : IORegistryEntry {
    NSDictionary* getProperties() override {
        return @{@"text": @"hello", @"utf8": @"é", @"small": [NSNumber numberWithUnsignedInt:0x12345678],
                 @"negative": [NSNumber numberWithInt:-3], @"wide": [NSNumber numberWithUnsignedLongLong:0x123456789abcdef0ULL],
                 @"flag": @YES, @"float": @1.25, @"array": @[], @"large": [NSMutableData dataWithLength:5000]};
    }
};
static void checkProperties(Fixture& fixture) {
    io_struct_inband_t buffer; mach_msg_type_number_t length;
    io_name_t key = "text";
    memset(buffer, 0x7e, sizeof(buffer)); length = 2;
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnIPCError);
    assert(length == 2 && buffer[0] == 0x7e);
    length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    assert(length == 6 && !memcmp(buffer, "hello", 6));
    strcpy(key, "small"); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    uint64_t small = 0; memcpy(&small, buffer, length); assert(length == CFNumberGetByteSize((CFNumberRef)fixture.getProperties()[@"small"]) && small == 0x12345678);
    strcpy(key, "negative"); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    int64_t negative = -1; memcpy(&negative, buffer, length); assert(length == CFNumberGetByteSize((CFNumberRef)fixture.getProperties()[@"negative"]) && negative == -3);
    strcpy(key, "wide"); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    uint64_t wide; memcpy(&wide, buffer, sizeof(wide)); assert(length == 8 && wide == 0x123456789abcdef0ULL);
    strcpy(key, "flag"); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    assert(length == 4 && !memcmp(buffer, "Yes", 4));
    strcpy(key, "utf8"); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnSuccess);
    assert(length == 3 && !memcmp(buffer, "\xc3\xa9", 3));
    strcpy(key, "large"); length = UINT32_MAX; memset(buffer, 0x7e, sizeof(buffer));
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnIPCError);
    assert(length == sizeof(buffer) && buffer[0] == 0x7e && buffer[sizeof(buffer)-1] == 0x7e);
    for (const char* name : {"float", "array", "missing"}) {
        strcpy(key, name); length = sizeof(buffer);
        auto expected = !strcmp(name,"missing") ? kIOReturnNoResources : kIOReturnBadArgument;
        assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == expected && length == 0);
    }
    memset(key, 'x', sizeof(key)); length = sizeof(buffer);
    assert(is_io_registry_entry_get_property_bytes(fixture.port(), key, buffer, &length) == kIOReturnBadArgument && length == 0);
}
struct ServiceFixture : IOService {
    NSDictionary* matchingDictionary() override {
        return @{@"IOProviderClass": @"ServiceFixture", @"IOClass": @"ServiceFixture"};
    }
};

static void checkGpuRegistry() {
    ServiceFixture service;
    uint64_t natural = service.id();
    assert(service.matches(@{@"IORegistryEntryID": @(natural)}));
    assert(!service.matches(@{@"IORegistryEntryID": @(natural + 1)}));
    assert(!service.matches(@{@"IORegistryEntryID": @"not a number"}));
    assert(service.assignId(0x100000777ULL));
    IORegistryEntry* found = IORegistryEntry::findById(0x100000777ULL);
    assert(found == &service);
    found->release();

    IOGraphicsAccelerator::registerSelf(ServiceRegistry::instance());
    IORegistryEntry* gpu = IORegistryEntry::findById(kIOGraphicsAcceleratorRegistryID);
    IOGraphicsAcceleratorIds::PciId expected = IOGraphicsAcceleratorIds::acceleratorIds();
    // An unknown identity publishes no entry, so Dawn falls back to the Metal
    // device name instead of reading vendor 0.
    if (expected.vendor == 0 && expected.device == 0) {
        assert(gpu == nullptr);
        return;
    }
    assert(gpu && !strcmp(gpu->className(), "IOGraphicsAccelerator2"));
    auto parents = gpu->getParents(kIOServicePlane);
    assert(parents.size() == 1 && !strcmp((*parents.begin())->className(), "IOPCIDevice"));
    NSData* vendor = (*parents.begin())->getProperties()[@"vendor-id"];
    NSData* device = (*parents.begin())->getProperties()[@"device-id"];
    assert([vendor length] == 4 && [device length] == 4);
    uint32_t vendorId = 0, deviceId = 0;
    memcpy(&vendorId, [vendor bytes], 4);
    memcpy(&deviceId, [device bytes], 4);
    // The entry must describe the device Metal selected: never another card's
    // IDs. Passthrough values are checked against the host identity, so any
    // vendor works; the two compatibility substitutions are validated separately.
    assert(vendorId == expected.vendor && deviceId == expected.device);
    bool known = false;
    for (const auto& card : IOGraphicsAcceleratorIds::hostCards())
        known = known || (card.vendor == vendorId && card.device == deviceId);
    known = known || (vendorId == 0x1002 && deviceId == 0x7340);
    known = known || (vendorId == 0x8086 && deviceId == 0x3e9b);
    assert(known);
    assert(vendorId != 0);
    gpu->release();

    // The pinned ID is exclusive, so no later entry can steal the accelerator's.
    ServiceFixture other;
    assert(!other.assignId(kIOGraphicsAcceleratorRegistryID));
}

static void checkIdentity() {
    NSString* uuid = @"00112233-4455-4677-8899-AABBCCDDEEFF";
    auto* valid = DarlingPlatformIdentity::properties(@{@"schema": @1, @"properties": @{@"model": @"ExampleModel1,1", @"provisioning_UDID": uuid, @"IOPlatformUUID": uuid, @"IOPlatformSerialNumber": @"SUPPLIED"}});
    assert(valid && [valid[@"IOPlatformUUID"] isEqual:uuid]);
    assert([valid[@"provisioning_UDID"] isEqual:uuid]);
    NSMutableDictionary* complete = [@{@"IOPlatformSerialNumber": @"SYNTHETIC123", @"IOPlatformUUID": uuid,
        @"provisioning_UDID": uuid, @"model": @"ExampleModel", @"product-name": @"ExampleModel",
        @"manufacturer": @"Example", @"board-id": @"Board", @"mlb-serial-number": @"SYNTHETIC-BOARD",
        @"ROM": [NSData dataWithBytes:"abcdef" length:6]} mutableCopy];
    complete[@"system-id"] = valid[@"system-id"];
    assert([complete count] == 10);
    assert(DarlingPlatformIdentity::properties(@{@"schema": @1, @"properties": complete}));
    [complete release];
    assert([valid[@"model"] isKindOfClass:[NSData class]] && [valid[@"model"] isEqual:valid[@"product-name"]]);
    const unsigned char bytes[] = {0x33,0x22,0x11,0,0x55,0x44,0x77,0x46,0x88,0x99,0xaa,0xbb,0xcc,0xdd,0xee,0xff};
    assert([valid[@"system-id"] isEqual:[NSData dataWithBytes:bytes length:16]]);
    for (NSDictionary* invalid in @[
        @{@"schema": @1, @"properties": @{@"provisioning_UDID": @"bad"}},
        @{@"schema": @1, @"properties": @{@"IOPlatformUUID": uuid, @"provisioning_UDID": @"11112233-4455-4677-8899-AABBCCDDEEFF"}},
        @{@"schema": @2, @"properties": @{}}, @{@"schema": @YES, @"properties": @{}},
        @{@"schema": @1, @"properties": @{@"unknown": @"value"}},
        @{@"schema": @1, @"properties": @{@"ROM": [NSData data]}},
        @{@"schema": @1, @"properties": @{@"IOPlatformUUID": @"00000000-0000-0000-0000-000000000000"}},
        @{@"schema": @1, @"properties": @{@"IOPlatformUUID": uuid, @"system-id": [NSMutableData dataWithLength:16]}},
        @{@"schema": @1, @"properties": @{@"model": @"bad\nmodel"}}
    ]) assert(!DarlingPlatformIdentity::properties(invalid));
    assert(!DarlingPlatformIdentity::load("/private/etc/does-not-exist-platform-test.plist"));
    // Exercise the same plist reader used by startup, using only a disposable file.
    char filename[] = "/tmp/darling-platform-probe.XXXXXX";
    int descriptor = mkstemp(filename); assert(descriptor >= 0);
    NSDictionary* config = @{@"schema": @1, @"properties": @{@"IOPlatformSerialNumber": @"FILE-FIXTURE", @"ROM": [NSData dataWithBytes:"abcdef" length:6]}};
    NSData* encoded = [NSPropertyListSerialization dataWithPropertyList:config format:NSPropertyListXMLFormat_v1_0 options:0 error:NULL];
    assert(encoded);
    FILE* output = fdopen(descriptor,"wb"); assert(output);
    assert(fwrite([encoded bytes],1,[encoded length],output) == [encoded length]); fclose(output);
    NSDictionary* loaded = DarlingPlatformIdentity::load(filename);
    assert(loaded && [loaded[@"IOPlatformSerialNumber"] isEqual:@"FILE-FIXTURE"] && [loaded[@"ROM"] length] == 6);
    unlink(filename);
}
int main() {
    @autoreleasepool {
        Fixture fixture;
        std::string temporaryPlane = "OwnedPlane";
        fixture.registerInPlane(temporaryPlane.c_str(), "Child", IORegistryEntry::root());
        temporaryPlane.assign(1000, 'x');
        assert(fixture.getPath("OwnedPlane") == "OwnedPlane:/Child");
        assert(IORegistryEntry::fromPath("OwnedPlane:/Child") == &fixture);
        assert(IORegistryEntry::fromPath("IOService:/") == IORegistryEntry::root());
        assert(!IORegistryEntry::fromPath("MissingPlane:/"));
        assert(!IORegistryEntry::fromPath("OwnedPlane:/child"));
        assert(!IORegistryEntry::fromPath("OwnedPlane:/../Child"));
        io_name_t plane = "OwnedPlane"; boolean_t inPlane = TRUE;
        assert(is_io_registry_entry_in_plane(fixture.port(), plane, &inPlane) == kIOReturnSuccess && inPlane);
        strcpy(plane, "Other");
        assert(is_io_registry_entry_in_plane(fixture.port(), plane, &inPlane) == kIOReturnSuccess && !inPlane);
        std::vector<std::unique_ptr<IORegistryEntry>> entries;
        IORegistryEntry* parent = &fixture;
        for (unsigned i=0; i<40; ++i) {
            auto entry = std::make_unique<IORegistryEntry>();
            entry->registerInPlane("OwnedPlane", ("Entry"+std::to_string(i)+std::string(110,'x')).c_str(), parent);
            parent = entry.get(); entries.push_back(std::move(entry));
        }
        auto longPath = parent->getPath("OwnedPlane"); assert(longPath.size() > sizeof(io_string_inband_t));
        assert(IORegistryEntry::fromPath(longPath) == parent);
        // An attempted cycle must not alter an existing root path.
        fixture.registerInPlane("OwnedPlane", "cycle", parent);
        assert(fixture.getPath("OwnedPlane") == "OwnedPlane:/Child");
        strcpy(plane, "OwnedPlane");
        io_string_t fixed; memset(fixed,'x',sizeof(fixed));
        assert(is_io_registry_entry_get_path(parent->port(), plane, fixed) == kIOReturnNoSpace && fixed[0] == 0);
        io_string_inband_t inlinePath = ""; io_buf_ptr_t out = nullptr; mach_msg_type_number_t outCount = 0;
        assert(is_io_registry_entry_get_path_ool(parent->port(), plane, inlinePath, &out, &outCount) == kIOReturnSuccess);
        assert(!inlinePath[0] && std::string(out,outCount) == longPath && outCount == longPath.size());
        for (unsigned i=0; i<1000; ++i) {
            vm_address_t allocation = 0;
            assert(vm_allocate(mach_task_self(), &allocation, longPath.size()+1, TRUE) == KERN_SUCCESS);
            memcpy((void*)allocation, longPath.c_str(), longPath.size()+1);
            kern_return_t result; mach_port_t found;
            assert(is_io_registry_entry_from_path_ool(0, inlinePath, (io_buf_ptr_t)allocation, longPath.size()+1, &result, &found) == kIOReturnSuccess);
            assert(result == kIOReturnSuccess && found == parent->port());
        }
        // Malformed OOL input must fail without publishing a port.
        vm_address_t bad = 0;
        assert(vm_allocate(mach_task_self(), &bad, 4, TRUE) == KERN_SUCCESS);
        memset((void*)bad, 'x', 4);
        kern_return_t badResult; mach_port_t badEntry = 123;
        assert(is_io_registry_entry_from_path_ool(0, inlinePath, (io_buf_ptr_t)bad, 4, &badResult, &badEntry) == kIOReturnSuccess);
        assert(badResult == kIOReturnBadArgument && badEntry == MACH_PORT_NULL);
        auto detachedParent = std::make_unique<IORegistryEntry>();
        auto detachedChild = std::make_unique<IORegistryEntry>();
        detachedParent->registerInPlane("OwnedPlane", "Parent", &fixture);
        detachedChild->registerInPlane("OwnedPlane", "Child", detachedParent.get());
        assert(detachedChild->inPlane("OwnedPlane"));
        detachedParent.reset();
        assert(!detachedChild->inPlane("OwnedPlane") && detachedChild->getPath("OwnedPlane") == "?unsup_plane?");
        auto deleted = std::make_unique<IORegistryEntry>();
        deleted->registerInPlane("OwnedPlane", "Deleted", &fixture);
        assert(IORegistryEntry::fromPath("OwnedPlane:/Child/Deleted") == deleted.get());
        deleted.reset();
        assert(!IORegistryEntry::fromPath("OwnedPlane:/Child/Deleted"));
        checkProperties(fixture); checkIdentity(); checkGpuRegistry();
        struct Serializable : IORegistryEntry {
            NSDictionary* getProperties() override { return @{@"text": @"value", @"data": [NSData dataWithBytes:"abc" length:3], @"number": @123, @"array": @[@YES, @"x"]}; }
        } serializable;
        io_buf_ptr_t serialized = nullptr; mach_msg_type_number_t serializedCount = 0;
        assert(is_io_registry_entry_get_properties_bin(serializable.port(), &serialized, &serializedCount) == kIOReturnSuccess);
        CFStringRef error = nullptr;
        CFTypeRef decoded = IOCFUnserializeWithSize(serialized, serializedCount, kCFAllocatorDefault, 0, &error);
        assert(decoded && !error && [(NSDictionary*)decoded isEqual:serializable.getProperties()]);
        CFRelease(decoded);
        vm_deallocate(mach_task_self(), (vm_address_t)serialized, serializedCount);
        std::thread([&fixture, parent, &serializable, longPath] {
            @autoreleasepool {
                io_name_t plane = "OwnedPlane";
                for (unsigned i=0; i<100; ++i) {
                    io_string_inband_t path = "";
                    io_buf_ptr_t bytes = nullptr; mach_msg_type_number_t length = 0;
                    assert(io_registry_entry_get_path_ool(parent->port(), plane, path, &bytes, &length) == kIOReturnSuccess);
                    assert(bytes && std::string(bytes,length) == longPath);
                    vm_deallocate(mach_task_self(), (vm_address_t)bytes, length);
                    mach_port_t found = 0; kern_return_t result;
                    assert(io_registry_entry_from_path_ool(fixture.port(), path, const_cast<char*>(longPath.c_str()), longPath.size()+1, &result, &found) == kIOReturnSuccess);
                    assert(result == kIOReturnSuccess && found == parent->port());
                    mach_port_deallocate(mach_task_self(), found);
                }
                io_name_t property = "flag"; io_struct_inband_t raw; mach_msg_type_number_t count = sizeof(raw);
                assert(io_registry_entry_get_property_bytes(fixture.port(), property, raw, &count) == kIOReturnSuccess && count == 4 && !memcmp(raw,"Yes",4));
                io_buf_ptr_t bytes = nullptr; count = 0;
                assert(io_registry_entry_get_properties_bin(serializable.port(), &bytes, &count) == kIOReturnSuccess);
                CFStringRef error = nullptr;
                CFTypeRef decoded = IOCFUnserializeWithSize(bytes, count, kCFAllocatorDefault, 0, &error);
                assert(decoded && !error && [(NSDictionary*)decoded isEqual:serializable.getProperties()]);
                CFRelease(decoded); vm_deallocate(mach_task_self(), (vm_address_t)bytes, count);
                puts("REGISTRY_COMPAT paths=1 planes=1 graph_lifetime=1 cycles=1 ool_roundtrips=1000 ipc_roundtrips=100 property_bounds=1 numeric_widths=1 serialization=1 identity_schema=1 gpu_registry=1 pass=1");
                fflush(stdout);
                _Exit(0);
            }
        }).detach();
        dispatch_main();
    }
}
