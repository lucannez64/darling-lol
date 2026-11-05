// Draws offscreen with each vertex step function and rate, then reads the result back.
// usage: riot-metal-step-rate-check <indium test/cubemap/shaders.metallib>
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>

static const float diagonal = 0.70710678f;
static const float normals[4][4] = {
    {diagonal, 0, diagonal, 0},
    {-diagonal, 0, diagonal, 0},
    {0, diagonal, diagonal, 0},
    {0, -diagonal, diagonal, 0},
};
static const unsigned elementFace[4] = {1, 0, 3, 2};
static const unsigned char faceColors[6][4] = {
    {255, 0, 0, 255}, {0, 255, 0, 255}, {0, 0, 255, 255},
    {255, 255, 0, 255}, {255, 0, 255, 255}, {0, 255, 255, 255},
};
static const unsigned char clearColor[4] = {0, 0, 0, 255};

struct Case {
    const char* name;
    MTLVertexStepFunction function;
    NSUInteger rate;
    NSUInteger instances;
    int element;
    bool sameNormals;
};

static const Case cases[] = {
    {"per-instance rate 1, 3 instances", MTLVertexStepFunctionPerInstance, 1, 3, 2, false},
    {"per-instance rate 2, 3 instances", MTLVertexStepFunctionPerInstance, 2, 3, 1, false},
    {"per-instance rate 2, 5 instances", MTLVertexStepFunctionPerInstance, 2, 5, 2, false},
    {"per-instance rate 3, 4 instances", MTLVertexStepFunctionPerInstance, 3, 4, 1, false},
    {"per-instance rate 0, 3 instances", MTLVertexStepFunctionPerInstance, 0, 3, 0, false},
    {"constant rate 0, 3 instances", MTLVertexStepFunctionConstant, 0, 3, 0, false},
    {"per-vertex rate 1", MTLVertexStepFunctionPerVertex, 1, 1, 3, true},
    {"per-vertex rate 0", MTLVertexStepFunctionPerVertex, 0, 1, 3, true},
    {"per-patch cannot be created, draw is skipped", MTLVertexStepFunctionPerPatch, 1, 1, -1, false},
};

static id<MTLRenderPipelineState> makePipeline(id<MTLDevice> device, id<MTLFunction> vertex, id<MTLFunction> fragment, MTLVertexStepFunction function, NSUInteger rate) {
    MTLRenderPipelineDescriptor* desc = [MTLRenderPipelineDescriptor new];
    desc.vertexFunction = vertex;
    desc.fragmentFunction = fragment;
    desc.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm;
    desc.vertexDescriptor = [MTLVertexDescriptor vertexDescriptor];
    desc.vertexDescriptor.attributes[0].format = MTLVertexFormatFloat4;
    desc.vertexDescriptor.attributes[0].bufferIndex = 2;
    desc.vertexDescriptor.layouts[2].stride = 16;
    desc.vertexDescriptor.attributes[1].format = MTLVertexFormatFloat4;
    desc.vertexDescriptor.attributes[1].bufferIndex = 3;
    desc.vertexDescriptor.layouts[3].stride = 16;
    desc.vertexDescriptor.layouts[3].stepFunction = function;
    desc.vertexDescriptor.layouts[3].stepRate = rate;
    NSError* error = nil;
    id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:desc error:&error];
    if (!pipeline) fprintf(stderr, "pipeline creation failed: %s\n", error.localizedDescription.UTF8String);
    return pipeline;
}

int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc != 2) { fprintf(stderr, "usage: %s shaders.metallib\n", argv[0]); return 2; }
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { fprintf(stderr, "no Metal device\n"); return 2; }
        NSData* shader = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
        if (!shader) { fprintf(stderr, "cannot read %s\n", argv[1]); return 2; }
        dispatch_data_t data = dispatch_data_create(shader.bytes, shader.length, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
        NSError* error = nil;
        id<MTLLibrary> library = [device newLibraryWithData:data error:&error];
        if (!library) { fprintf(stderr, "library failed: %s\n", error.localizedDescription.UTF8String); return 2; }
        id<MTLFunction> vertex = [library newFunctionWithName:@"vertex_reflect"];
        id<MTLFunction> fragment = [library newFunctionWithName:@"fragment_cube_lookup"];
        if (!vertex || !fragment) { fprintf(stderr, "shader functions missing\n"); return 2; }

        id<MTLCommandQueue> queue = [device newCommandQueue];
        MTLTextureDescriptor* cubeDesc = [MTLTextureDescriptor textureCubeDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm size:4 mipmapped:NO];
        cubeDesc.storageMode = MTLStorageModePrivate;
        cubeDesc.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> cube = [device newTextureWithDescriptor:cubeDesc];
        if (!cube) { fprintf(stderr, "cube texture failed\n"); return 2; }
        printf("cube texture type=%lu size=%lux%lu\n", (unsigned long) cube.textureType, (unsigned long) cube.width, (unsigned long) cube.height);
        unsigned char facePixels[6][4 * 4 * 4];
        for (int face = 0; face < 6; ++face) for (int p = 0; p < 16; ++p) memcpy(facePixels[face] + p * 4, faceColors[face], 4);
        id<MTLBuffer> faceBuffer = [device newBufferWithBytes:facePixels length:sizeof(facePixels) options:MTLResourceStorageModeShared];
        id<MTLCommandBuffer> upload = [queue commandBuffer];
        id<MTLBlitCommandEncoder> uploadBlit = [upload blitCommandEncoder];
        for (NSUInteger face = 0; face < 6; ++face) {
            [uploadBlit copyFromBuffer:faceBuffer sourceOffset:face * 64 sourceBytesPerRow:16 sourceBytesPerImage:64 sourceSize:MTLSizeMake(4, 4, 1) toTexture:cube destinationSlice:face destinationLevel:0 destinationOrigin:MTLOriginMake(0, 0, 0)];
        }
        [uploadBlit endEncoding];
        [upload commit];
        [upload waitUntilCompleted];
        id<MTLSamplerState> sampler = [device newSamplerStateWithDescriptor:[MTLSamplerDescriptor new]];
        if (!sampler) { fprintf(stderr, "sampler failed\n"); return 2; }

        const float positions[3][4] = {{-1, -1, 0.5f, 1}, {3, -1, 0.5f, 1}, {-1, 3, 0.5f, 1}};
        id<MTLBuffer> positionBuffer = [device newBufferWithBytes:positions length:sizeof(positions) options:MTLResourceStorageModeShared];
        id<MTLBuffer> normalBuffer = [device newBufferWithBytes:normals length:sizeof(normals) options:MTLResourceStorageModeShared];
        const float same[3][4] = {{normals[3][0], normals[3][1], normals[3][2], 0}, {normals[3][0], normals[3][1], normals[3][2], 0}, {normals[3][0], normals[3][1], normals[3][2], 0}};
        id<MTLBuffer> sameBuffer = [device newBufferWithBytes:same length:sizeof(same) options:MTLResourceStorageModeShared];
        float uniforms[68] = {0};
        for (int matrix = 0; matrix < 4; ++matrix) for (int i = 0; i < 4; ++i) uniforms[matrix * 16 + i * 5] = 1;
        uniforms[66] = -1000; uniforms[67] = 1;
        id<MTLBuffer> uniformBuffer = [device newBufferWithBytes:uniforms length:sizeof(uniforms) options:MTLResourceStorageModeShared];

        MTLTextureDescriptor* targetDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:16 mipmapped:NO];
        targetDesc.storageMode = MTLStorageModePrivate;
        targetDesc.usage = MTLTextureUsageRenderTarget;

        __block unsigned failures = 0;
        unsigned total = 0;
        auto run = ^(const char* name, id<MTLRenderPipelineState> first, id<MTLRenderPipelineState> second, id<MTLBuffer> elements, NSUInteger instances, const unsigned char* expected) {
            id<MTLTexture> target = [device newTextureWithDescriptor:targetDesc];
            id<MTLBuffer> readback = [device newBufferWithLength:16 * 16 * 4 options:MTLResourceStorageModeShared];
            MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
            pass.colorAttachments[0].texture = target;
            pass.colorAttachments[0].loadAction = MTLLoadActionClear;
            pass.colorAttachments[0].storeAction = MTLStoreActionStore;
            pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1);
            id<MTLCommandBuffer> commands = [queue commandBuffer];
            id<MTLRenderCommandEncoder> render = [commands renderCommandEncoderWithDescriptor:pass];
            [render setVertexBuffer:uniformBuffer offset:0 atIndex:1];
            [render setVertexBuffer:positionBuffer offset:0 atIndex:2];
            [render setVertexBuffer:elements offset:0 atIndex:3];
            [render setFragmentBuffer:uniformBuffer offset:0 atIndex:0];
            [render setFragmentTexture:cube atIndex:0];
            [render setFragmentSamplerState:sampler atIndex:0];
            [render setRenderPipelineState:first];
            [render drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3 instanceCount:instances];
            if (second) {
                [render setRenderPipelineState:second];
                [render drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3 instanceCount:instances];
            }
            [render endEncoding];
            id<MTLBlitCommandEncoder> blit = [commands blitCommandEncoder];
            [blit copyFromTexture:target sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(16, 16, 1) toBuffer:readback destinationOffset:0 destinationBytesPerRow:16 * 4 destinationBytesPerImage:16 * 16 * 4];
            [blit endEncoding];
            [commands commit];
            [commands waitUntilCompleted];
            const unsigned char* pixel = (const unsigned char*) readback.contents + (8 * 16 + 8) * 4;
            bool ok = memcmp(pixel, expected, 4) == 0;
            if (!ok) ++failures;
            printf("%s: %s got=%u,%u,%u,%u want=%u,%u,%u,%u\n", ok ? "ok" : "FAIL", name, pixel[0], pixel[1], pixel[2], pixel[3], expected[0], expected[1], expected[2], expected[3]);
            fflush(stdout);
        };

        for (const Case& test: cases) {
            ++total;
            id<MTLRenderPipelineState> pipeline = makePipeline(device, vertex, fragment, test.function, test.rate);
            if (!pipeline) { ++failures; printf("FAIL: %s: no pipeline\n", test.name); continue; }
            const unsigned char* expected = test.element < 0 ? clearColor : faceColors[elementFace[test.element]];
            id<MTLBuffer> elements = test.sameNormals ? sameBuffer : normalBuffer;
            run(test.name, pipeline, nil, elements, test.instances, expected);
        }
        ++total;
        NSArray* attributes = vertex.vertexAttributes;
        MTLVertexAttribute* position = attributes.count == 2 ? attributes[0] : nil;
        MTLVertexAttribute* normal = attributes.count == 2 ? attributes[1] : nil;
        const bool reported = position.attributeIndex == 0 && position.attributeType == MTLDataTypeFloat4 && position.active && normal.attributeIndex == 1 && normal.attributeType == MTLDataTypeFloat4 && normal.active;
        if (!reported) ++failures;
        printf("%s: vertex function reports its attributes count=%lu\n", reported ? "ok" : "FAIL", (unsigned long) attributes.count);
        id<MTLRenderPipelineState> broken = makePipeline(device, vertex, fragment, MTLVertexStepFunctionPerPatch, 1);
        id<MTLRenderPipelineState> working = makePipeline(device, vertex, fragment, MTLVertexStepFunctionPerInstance, 2);
        ++total;
        run("a working pipeline still draws after a failed one", broken, working, normalBuffer, 3, faceColors[elementFace[1]]);
        printf("STEP_RATE_CHECK failures=%u cases=%u pass=%d\n", failures, total, failures == 0);
        return failures == 0 ? 0 : 1;
    }
}
