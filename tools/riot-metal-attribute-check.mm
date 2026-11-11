#import <Metal/Metal.h>
#import <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#undef require

static void require(bool condition, const char* message) {
    if (!condition) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
}

static MTLAttribute* findStageInput(NSArray<MTLAttribute*>* inputs, NSUInteger attributeIndex) {
    for (MTLAttribute* input in inputs) {
        if (input.attributeIndex == attributeIndex) return input;
    }
    return nil;
}

static void checkStageInputMatching(NSArray<MTLAttribute*>* inputs) {
    MTLAttribute* position = findStageInput(inputs, 0);
    MTLAttribute* normal = findStageInput(inputs, 1);
    require(position != nil && normal != nil, "stage input matching fixtures exist");
    NSArray<MTLAttribute*>* reordered = @[normal, position];
    require(findStageInput(reordered, 0) == position, "reordered position matched by index");
    require(findStageInput(reordered, 1) == normal, "reordered normal matched by index");
    NSArray<MTLAttribute*>* sparse = @[normal];
    require(findStageInput(sparse, 1) == normal, "sparse input matched beyond array bounds");
    require(findStageInput(sparse, 0) == nil, "missing sparse input returns nil");
    require(findStageInput(reordered, 2) == nil, "missing trailing input returns nil");
    require(findStageInput(@[], 0) == nil, "empty input array returns nil");
    fprintf(stderr, "PASS: stage inputs match by index for reordered, sparse and missing inputs\n");
}

int main(int argc, char** argv) {
    @autoreleasepool {
        require(argc == 2, "expected cubemap metallib path");
        NSData* bytes = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
        require(bytes != nil, "read cubemap metallib");
        dispatch_data_t data = dispatch_data_create(bytes.bytes, bytes.length, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
        NSError* error = nil;
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLLibrary> library = [device newLibraryWithData:data error:&error];
        dispatch_release(data);
        require(library != nil && error == nil, "load shader library");
        id<MTLFunction> vertex = [library newFunctionWithName:@"vertex_reflect"];
        require(vertex != nil, "find vertex function");
        NSArray<MTLVertexAttribute*>* attributes = vertex.vertexAttributes;
        require(vertex.stageInputAttributes.count == 2, "stage input attributes reflected");
        require(attributes.count == 2, "two active vertex inputs");
        bool position = false, normal = false;
        for (MTLVertexAttribute* attribute in attributes) {
            MTLAttribute* stage = findStageInput(vertex.stageInputAttributes, attribute.attributeIndex);
            require(stage != nil, "matching stage input exists");
            require(stage.attributeIndex == attribute.attributeIndex && stage.attributeType == attribute.attributeType &&
                    [stage.name isEqualToString:attribute.name] && stage.active, "stage input metadata agrees");
            require(attribute.active && attribute.attributeType == MTLDataTypeFloat4, "active float4 attribute");
            if (attribute.attributeIndex == 0) position = [attribute.name isEqualToString:@"position"];
            else if (attribute.attributeIndex == 1) normal = [attribute.name isEqualToString:@"normal"];
            else require(false, "unexpected attribute index");
        }
        require(position && normal, "indices 0 and 1 reflected");
        checkStageInputMatching(vertex.stageInputAttributes);
        id<MTLFunction> fragment = [library newFunctionWithName:@"fragment_cube_lookup"];
        require(fragment != nil && fragment.vertexAttributes == nil, "fragment has no vertex attributes");
        [fragment release]; [vertex release]; [library release];
        fprintf(stderr, "PASS: vertex shader attributes retain real indices and types\n");
    }
}
