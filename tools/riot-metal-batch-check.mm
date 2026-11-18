#import <Metal/Metal.h>
#import <Metal/MTLDeviceInternal.h>
#include <indium/command-encoder.private.hpp>
#import <Metal/MTLDepthStencilInternal.h>
#import <Metal/MTLVertexDescriptorInternal.h>
#import <Metal/MTLRenderPipelineInternal.h>
#import <Metal/MTLRenderCommandEncoderInternal.h>
#import <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <stdio.h>
#include <stdlib.h>
static void verifyBatch(bool value, const char* message) {
    if (!value) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
}
static void checkCopiedConstants(id<MTLDevice> device) {
    auto backend = std::dynamic_pointer_cast<Indium::PrivateDevice>(((MTLDeviceInternal*)device).device);
    Indium::FunctionResources resources;
    const unsigned values[] = {16,8}, changed[] = {32,16};
    resources.setBytes(backend,values,sizeof(values),0);
    auto first=resources.buffers[0];
    resources.setBytes(backend,values,sizeof(values),0);
    verifyBatch(resources.buffers[0]==first,"identical copied constants retain their address");
    resources.setBytes(backend,changed,sizeof(changed),0);
    verifyBatch(resources.buffers[0].second!=first.second,"changed copied constants use immutable storage");
    verifyBatch(!memcmp((char*)first.first->contents()+first.second,values,sizeof(values)),"earlier constants survive later uploads");
    Indium::FunctionInfo info{};
    Iridium::BindingInfo binding{};binding.type=Iridium::BindingType::Buffer;binding.index=0;
    info.bindings.push_back(binding);
    std::vector<std::shared_ptr<Indium::Buffer>> keep;
    Indium::AddressPool pool;
    auto table=Indium::makeBufferAddressTable(resources,info,nullptr,backend,keep,pool);
    resources.setBytes(backend,values,sizeof(values),15); // not used by this shader
    auto repeated=Indium::makeBufferAddressTable(resources,info,nullptr,backend,keep,pool,&table);
    verifyBatch(repeated.buffer==table.buffer && repeated.offset==table.offset,"unused binding changes retain address table");
    auto oldAddress=*(uint64_t*)((char*)pool.chunk->contents()+table.offset);
    resources.setBufferOffset(first.second,0);
    auto next=Indium::makeBufferAddressTable(resources,info,nullptr,backend,keep,pool,&table);
    verifyBatch(next.offset!=table.offset,"used binding change allocates a new immutable address table");
    verifyBatch(*(uint64_t*)((char*)pool.chunk->contents()+table.offset)==oldAddress,"earlier address table remains immutable");
    fprintf(stderr,"PASS: copied constants and immutable address-table reuse/invalidation\n");
}
void checkMetalBatch(id<MTLDevice> device, id<MTLSamplerState> sampler) {
    checkCopiedConstants(device);
    MTLVertexDescriptor* vertex = [MTLVertexDescriptor vertexDescriptor];
    vertex.attributes[2].format = MTLVertexFormatFloat3;
    vertex.attributes[2].bufferIndex = 4;
    vertex.attributes[2].offset = 16;
    vertex.layouts[4].stride = 32;
    vertex.layouts[4].stepFunction = MTLVertexStepFunctionPerInstance;
    vertex.layouts[4].stepRate = 2;
    MTLVertexDescriptor* vertexCopy = [vertex copy];
    [vertex reset];
    verifyBatch(vertex.attributes[2].format == MTLVertexFormatInvalid && vertexCopy.attributes[2].format == MTLVertexFormatFloat3, "vertex reset/copy independence");
    auto layout = [vertexCopy asIndiumDescriptor];
    verifyBatch(layout.attributes.at(2).offset == 16 && layout.layouts.at(4).stride == 32 && layout.layouts.at(4).stepRate == 2, "vertex layout conversion");
    MTLRenderPipelineDescriptor* pipeline = [MTLRenderPipelineDescriptor new];
    pipeline.vertexDescriptor = vertexCopy;
    auto pipelineDescriptor = [pipeline asIndiumDescriptor];
    verifyBatch(pipelineDescriptor.vertexDescriptor && pipelineDescriptor.vertexDescriptor->attributes.at(2).bufferIndex == 4, "pipeline receives vertex layout");
    [pipeline release]; [vertexCopy release];
    MTLScissorRect scissor = {3, 5, 7, 11};
    auto translatedScissor = MTLScissorRectToIndium(scissor);
    verifyBatch(translatedScissor.x == 3 && translatedScissor.y == 5 && translatedScissor.width == 7 && translatedScissor.height == 11, "scissor binary layout");
    MTLDepthStencilDescriptor* depth = [MTLDepthStencilDescriptor new];
    verifyBatch(depth.depthCompareFunction == MTLCompareFunctionAlways && !depth.depthWriteEnabled, "depth defaults");
    MTLStencilDescriptor* stencil = [MTLStencilDescriptor new];
    stencil.stencilCompareFunction = MTLCompareFunctionEqual;
    stencil.depthStencilPassOperation = MTLStencilOperationReplace;
    stencil.readMask = 0x7f; stencil.writeMask = 0x3f;
    depth.depthCompareFunction = MTLCompareFunctionLessEqual; depth.depthWriteEnabled = YES;
    depth.frontFaceStencil = stencil; depth.backFaceStencil = stencil;
    depth.label = @"depth/stencil test";
    stencil.readMask = 0;
    verifyBatch(depth.frontFaceStencil.readMask == 0x7f, "stencil setter copies descriptor");
    id<MTLDepthStencilState> state = [device newDepthStencilStateWithDescriptor:depth];
    verifyBatch(state && state.device == device && ((MTLDepthStencilStateInternal*)state).state, "real depth/stencil backend state");
    [stencil release]; [depth release];
    MTLTextureDescriptor* td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:8 mipmapped:NO];
    td.storageMode = MTLStorageModePrivate; td.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    id<MTLTexture> color = [device newTextureWithDescriptor:td];
    td.pixelFormat = device.isDepth24Stencil8PixelFormatSupported ? MTLPixelFormatDepth24Unorm_Stencil8 : MTLPixelFormatDepth32Float_Stencil8;
    td.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> depthTexture = [device newTextureWithDescriptor:td];
    verifyBatch(depthTexture != nil, "depth/stencil texture allocation");
    MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
    verifyBatch(pass.depthAttachment && pass.stencilAttachment, "render pass supplies attachment descriptors");
    pass.colorAttachments[0].texture = color;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(0.2,0.4,0.6,1);
    pass.depthAttachment.texture = depthTexture; pass.stencilAttachment.texture = depthTexture;
    pass.depthAttachment.loadAction = MTLLoadActionClear; pass.depthAttachment.storeAction = MTLStoreActionStore;
    pass.depthAttachment.clearDepth = 0.75;
    pass.stencilAttachment.loadAction = MTLLoadActionClear; pass.stencilAttachment.storeAction = MTLStoreActionStore;
    pass.stencilAttachment.clearStencil = 7;
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> commands = [queue commandBuffer];
    verifyBatch(commands.status == MTLCommandBufferStatusNotEnqueued, "initial command buffer status");
    [commands enqueue];
    verifyBatch(commands.status == MTLCommandBufferStatusEnqueued, "enqueued command buffer status");
    id<MTLRenderCommandEncoder> render = [commands renderCommandEncoderWithDescriptor:pass];
    verifyBatch(render != nil, "combined depth/stencil render encoder");
    [render setDepthStencilState:state]; [render setDepthBias:0 slopeScale:0 clamp:0]; [render setStencilReferenceValue:7];
    [render setVertexTexture:color atIndex:0]; [render setFragmentTexture:color atIndex:0];
    [render setVertexSamplerState:sampler atIndex:0]; [render setFragmentSamplerState:sampler atIndex:0];
    [render memoryBarrierWithScope:(MTLBarrierScopeTextures | MTLBarrierScopeRenderTargets) afterStages:MTLRenderStageFragment beforeStages:MTLRenderStageFragment];
    [render setDepthStencilState:nil]; [render endEncoding];
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block unsigned callbackState = 0;
    [commands addScheduledHandler:^(id<MTLCommandBuffer> buffer) { verifyBatch(buffer == commands, "scheduled buffer identity"); callbackState = 1; }];
    [commands addCompletedHandler:^(id<MTLCommandBuffer> buffer) { verifyBatch(callbackState == 1 && buffer == commands, "scheduled/completed handler order"); callbackState = 2; dispatch_semaphore_signal(done); }];
    [commands commit];
    [commands waitUntilScheduled];
    verifyBatch(commands.status >= MTLCommandBufferStatusScheduled, "scheduled command buffer status");
    [commands waitUntilCompleted];
    verifyBatch(commands.status == MTLCommandBufferStatusCompleted && commands.error == nil, "completed command buffer status");
    verifyBatch(commands.GPUStartTime > 0.0 && commands.GPUEndTime >= commands.GPUStartTime, "GPU start/end timestamps");
    verifyBatch(commands.kernelStartTime > 0.0 && commands.kernelEndTime >= commands.kernelStartTime, "kernel start/end timestamps");
    verifyBatch(dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) == 0 && callbackState == 2, "GPU handlers complete");
    dispatch_release(done);
    id<MTLBuffer> output = [device newBufferWithLength:512 options:MTLResourceStorageModeShared];
    commands = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [commands blitCommandEncoder];
    [blit copyFromTexture:color sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(16,8,1) toBuffer:output destinationOffset:0 destinationBytesPerRow:64 destinationBytesPerImage:512];
    [blit endEncoding]; [commands commit]; [commands waitUntilCompleted];
    unsigned char* pixels = (unsigned char*)output.contents;
    for (unsigned i = 0; i < 512; i += 4) verifyBatch(pixels[i]==51 && pixels[i+1]==102 && pixels[i+2]==153 && pixels[i+3]==255, "GPU render clear/readback");
    commands = [queue commandBuffer];
    id<MTLComputeCommandEncoder> compute = [commands computeCommandEncoder];
    [compute setTexture:color atIndex:0]; [compute setSamplerState:sampler atIndex:0];
    [compute setSamplerState:sampler lodMinClamp:0 lodMaxClamp:1 atIndex:1];
    [compute endEncoding]; [commands commit]; [commands waitUntilCompleted];
    [output release]; [depthTexture release]; [color release]; [queue release]; [state release];
    MTLTextureDescriptor* mipDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:8 mipmapped:YES];
    mipDesc.textureType = MTLTextureType2DArray; mipDesc.arrayLength = 2;
    mipDesc.storageMode = MTLStorageModePrivate; mipDesc.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    id<MTLTexture> mipTexture = [device newTextureWithDescriptor:mipDesc];
    id<MTLTexture> mipView = [mipTexture newTextureViewWithPixelFormat:mipDesc.pixelFormat textureType:MTLTextureType2D levels:NSMakeRange(1,3) slices:NSMakeRange(1,1)];
    verifyBatch(mipView.width == 8 && mipView.height == 4, "mip view dimensions shrink");
    id<MTLTexture> nestedView = [mipView newTextureViewWithPixelFormat:mipDesc.pixelFormat textureType:MTLTextureType2D levels:NSMakeRange(1,1) slices:NSMakeRange(0,1)];
    verifyBatch(nestedView.width == 4 && nestedView.height == 2, "nested mip dimensions shrink");
    id<MTLTexture> sameView = [mipView newTextureViewWithPixelFormat:mipDesc.pixelFormat];
    verifyBatch(sameView.width == 8 && sameView.mipmapLevelCount == 3, "full view preserves relative mip range");
    queue = [device newCommandQueue]; commands = [queue commandBuffer];
    pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = mipTexture; pass.colorAttachments[0].level = 1; pass.colorAttachments[0].slice = 1;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear; pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(0,1,0,1);
    render = [commands renderCommandEncoderWithDescriptor:pass]; [render endEncoding];
    pass.colorAttachments[0].texture = nestedView; pass.colorAttachments[0].level = 0; pass.colorAttachments[0].slice = 0;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(0,0,1,1);
    render = [commands renderCommandEncoderWithDescriptor:pass]; [render endEncoding];
    output = [device newBufferWithLength:160 options:MTLResourceStorageModeShared];
    blit = [commands blitCommandEncoder];
    [blit copyFromTexture:mipTexture sourceSlice:1 sourceLevel:1 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(8,4,1) toBuffer:output destinationOffset:0 destinationBytesPerRow:32 destinationBytesPerImage:128];
    [blit copyFromTexture:mipTexture sourceSlice:1 sourceLevel:2 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(4,2,1) toBuffer:output destinationOffset:128 destinationBytesPerRow:16 destinationBytesPerImage:32];
    [blit endEncoding]; [commands commit]; [commands waitUntilCompleted];
    pixels = (unsigned char*)output.contents;
    for (unsigned i = 0; i < 128; i += 4) verifyBatch(pixels[i]==0 && pixels[i+1]==255 && pixels[i+2]==0 && pixels[i+3]==255, "render targets selected mip and array slice");
    for (unsigned i = 128; i < 160; i += 4) verifyBatch(pixels[i]==0 && pixels[i+1]==0 && pixels[i+2]==255 && pixels[i+3]==255, "nested view targets correct underlying mip");
    [output release]; [queue release]; [sameView release]; [nestedView release]; [mipView release]; [mipTexture release];
    fprintf(stderr,"PASS: render attachment mip/slice and nested view pixel checks\n");
    fprintf(stderr, "PASS: Metal vertex layouts, depth/stencil, resource binding, render clear and handlers\n");
}

void checkMetalShader(id<MTLDevice> device) {
    if (getenv("RCLOL_CAPTURE_ONE_SUBMIT")) {
        FILE* request = fopen("/Volumes/SystemRoot@PROJECT_ROOT@/build/renderdoc/capture-request", "w");
        verifyBatch(request != nullptr, "capture request file"); fprintf(request,"0\n"); fclose(request);
    }
    const char* shaderPath = getenv("RIOT_METAL_SHADER");
    if (!shaderPath) return;
    NSData* shader = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:shaderPath]];
    verifyBatch(shader != nil, "shader fixture available");
    dispatch_data_t data = dispatch_data_create(shader.bytes, shader.length, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT,0), DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    NSError* error = nil;
    fprintf(stderr, "CHECK: shader library translation\n");
    id<MTLLibrary> library = [device newLibraryWithData:data error:&error];
    dispatch_release(data);
    verifyBatch(library != nil && error == nil, "dispatch-data shader library");
    id<MTLFunction> vertex = [library newFunctionWithName:@"vertexShader"];
    id<MTLFunction> fragment = [library newFunctionWithName:@"fragmentShader"];
    verifyBatch(vertex && fragment, "shader functions");
    verifyBatch(vertex.functionType == MTLFunctionTypeVertex && fragment.functionType == MTLFunctionTypeFragment, "shader function types");
    MTLRenderPipelineDescriptor* desc = [MTLRenderPipelineDescriptor new];
    desc.vertexFunction = vertex; desc.fragmentFunction = fragment;
    bool vertexDepth = getenv("RIOT_METAL_VERTEX_DEPTH") != nullptr;
    if (vertexDepth) {
        desc.vertexDescriptor = [MTLVertexDescriptor vertexDescriptor];
        desc.vertexDescriptor.attributes[7].format = MTLVertexFormatUChar4;
        desc.vertexDescriptor.attributes[7].bufferIndex = 2;
        desc.vertexDescriptor.layouts[2].stride = 4;
        desc.vertexDescriptor.layouts[2].stepFunction = MTLVertexStepFunctionPerVertex;
    }
    NSUInteger colorSlot = getenv("RIOT_METAL_COLOR_SLOT") ? strtoul(getenv("RIOT_METAL_COLOR_SLOT"), nullptr, 10) : 0;
    verifyBatch(colorSlot < 8, "valid test color slot");
    desc.colorAttachments[colorSlot].pixelFormat = MTLPixelFormatRGBA8Unorm;
    MTLRenderPipelineReflection* reflection = nil;
    id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:desc options:(MTLPipelineOptionArgumentInfo | MTLPipelineOptionBufferTypeInfo) reflection:&reflection error:&error];
    fprintf(stderr,"CHECK: pipeline=%d reflection=%d vertexArgs=%lu fragmentArgs=%lu\n", pipeline != nil, reflection != nil, (unsigned long)reflection.vertexArguments.count, (unsigned long)reflection.fragmentArguments.count);
    verifyBatch(pipeline && reflection && reflection.vertexArguments.count == (vertexDepth ? 3 : 2) && reflection.fragmentArguments.count == (getenv("RIOT_METAL_TWO_BUFFER_STAGES") ? 2 : (vertexDepth ? 1 : 0)), "shader reflection populated");
    MTLArgument* argument = reflection.vertexArguments[0];
    verifyBatch(argument.type == MTLArgumentTypeBuffer && argument.index == 0 && [argument.name isEqualToString:@"vertices"] && argument.bufferDataSize == 32 && argument.bufferAlignment == 16, "AIR buffer reflection metadata");
    verifyBatch(argument.bufferStructType.members.count == 2 && [argument.bufferStructType memberByName:@"color"].offset == 16, "AIR struct reflection metadata");
    MTLTextureDescriptor* td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:16 height:8 mipmapped:NO];
    td.storageMode = MTLStorageModePrivate; td.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> color = [device newTextureWithDescriptor:td];
    struct Vertex { float position[2]; float padding[2]; float color[4]; } vertices[] = {
        {{-16,-8},{0,0},{1,0,0,1}},{{16,-8},{0,0},{1,0,0,1}},{{0,8},{0,0},{1,0,0,1}}};
    unsigned viewport[] = {16,8};
    Vertex both[2][3];
    memcpy(both[0], vertices, sizeof(vertices));
    memcpy(both[1], vertices, sizeof(vertices));
    for (auto& v: both[1]) { v.color[0] = 0; v.color[1] = 1; }
    id<MTLBuffer> vb = [device newBufferWithBytes:both length:sizeof(both) options:MTLResourceStorageModeShared];
    unsigned char attributeBytes[12] = {255,128,7,1,255,128,7,1,255,128,7,1};
    id<MTLBuffer> attributes = vertexDepth ? [device newBufferWithBytes:attributeBytes length:sizeof(attributeBytes) options:MTLResourceStorageModeShared] : nil;
    MTLTextureDescriptor* depthDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatDepth32Float_Stencil8 width:16 height:8 mipmapped:NO];
    depthDesc.storageMode = MTLStorageModePrivate; depthDesc.usage = MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget;
    id<MTLTexture> sampledDepth = vertexDepth ? [device newTextureWithDescriptor:depthDesc] : nil;
    id<MTLBuffer> uniforms = [device newBufferWithBytes:viewport length:sizeof(viewport) options:MTLResourceStorageModeShared];
    const float tints[8] = {0,0,0,0,1,1,1,1};
    id<MTLBuffer> fragmentUniforms = getenv("RIOT_METAL_TWO_BUFFER_STAGES") ? [device newBufferWithBytes:tints length:sizeof(tints) options:MTLResourceStorageModeShared] : nil;
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> commands = [queue commandBuffer];
    if (vertexDepth) {
        MTLRenderPassDescriptor* depthClear = [MTLRenderPassDescriptor renderPassDescriptor];
        depthClear.depthAttachment.texture = sampledDepth;
        depthClear.depthAttachment.loadAction = MTLLoadActionClear;
        depthClear.depthAttachment.storeAction = MTLStoreActionStore;
        depthClear.depthAttachment.clearDepth = 0.75;
        id<MTLRenderCommandEncoder> clear = [commands renderCommandEncoderWithDescriptor:depthClear];
        [clear endEncoding];
    }
    MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[colorSlot].texture = color; pass.colorAttachments[colorSlot].loadAction = MTLLoadActionClear;
    pass.colorAttachments[colorSlot].storeAction = MTLStoreActionStore;
    pass.colorAttachments[colorSlot].clearColor = MTLClearColorMake(0,0,0,1);
    id<MTLRenderCommandEncoder> render = [commands renderCommandEncoderWithDescriptor:pass];
    [render setRenderPipelineState:pipeline];
    if (vertexDepth) { [render setVertexBuffer:attributes offset:0 atIndex:2]; [render setFragmentTexture:sampledDepth atIndex:0]; }
    [render setVertexBuffer:vb offset:0 atIndex:0]; [render setVertexBuffer:uniforms offset:0 atIndex:1];
    if (fragmentUniforms) [render setFragmentBuffer:fragmentUniforms offset:16 atIndex:0];
    [render setScissorRect:(MTLScissorRect){0,0,16,8}];
    // Exercise descriptor reuse, then pipeline invalidation and pool rollover.
    const bool bindingBench = getenv("RIOT_METAL_BINDING_BENCH") != nullptr;
    const unsigned repeatedDraws = bindingBench ? 10000 : 100;
    for (unsigned draw = 0; draw < 100; ++draw) {
        // Identical copied uniforms and unused-slot changes must preserve pixels.
        // The first draw also changes data, so stale constant reuse fails readback.
        unsigned wrongViewport[] = {32,16};
        [render setVertexBytes:wrongViewport length:sizeof(wrongViewport) atIndex:1];
        [render setVertexBytes:viewport length:sizeof(viewport) atIndex:1];
        [render setVertexBytes:viewport length:sizeof(viewport) atIndex:1];
        [render setVertexBuffer:vb offset:(draw & 1) * sizeof(vertices) atIndex:15];
        [render drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    }
    id<MTLRenderPipelineState> nextPipeline = [device newRenderPipelineStateWithDescriptor:desc error:&error];
    verifyBatch(nextPipeline && error == nil, "second render pipeline");
    [pipeline release];
    pipeline = nextPipeline;
    [render setRenderPipelineState:pipeline];
    const uint64_t bindingStart = bindingBench ? mach_absolute_time() : 0;
    for (unsigned draw = 0; draw < repeatedDraws; ++draw) {
        if (getenv("RIOT_METAL_REBIND_BENCH")) {
            [render setVertexBuffer:vb offset:(draw & 1) * sizeof(vertices) atIndex:0];
            if (vertexDepth) [render setFragmentTexture:sampledDepth atIndex:0];
        } else {
        [render setVertexBufferOffset:(draw & 1) * sizeof(vertices) atIndex:0];
        }
        if (fragmentUniforms) [render setFragmentBufferOffset:(draw & 1) * 16 atIndex:0];
        [render drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    }
    if (bindingBench) {
        uint64_t elapsed = mach_absolute_time() - bindingStart;
        mach_timebase_info_data_t scale; mach_timebase_info(&scale);
        fprintf(stderr, "BINDING_BENCH: %u changed-offset draws encoding_ms=%.3f\n",
                repeatedDraws, elapsed * (double)scale.numer / scale.denom / 1e6);
    }
    if (fragmentUniforms) [render setFragmentBufferOffset:16 atIndex:0];
    // Indexed draws exercise reused handles, offset changes, and type changes.
    const uint16_t indices16[] = {0,0,1,2};
    const uint32_t indices32[] = {0,0,1,2};
    id<MTLBuffer> ib16 = [device newBufferWithBytes:indices16 length:sizeof(indices16) options:MTLResourceStorageModeShared];
    id<MTLBuffer> ib32 = [device newBufferWithBytes:indices32 length:sizeof(indices32) options:MTLResourceStorageModeShared];
    for (unsigned draw = 0; draw < 100; ++draw) {
        const bool wide = (draw / 2) & 1;
        [render drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:3 indexType:(wide ? MTLIndexTypeUInt32 : MTLIndexTypeUInt16) indexBuffer:(wide ? ib32 : ib16) indexBufferOffset:(wide ? sizeof(uint32_t) : sizeof(uint16_t))];
    }
    // Release both wrappers before submission, replacing the first binding.
    // The encoder must retain the first draw's buffer after the second bind.
    id<MTLBuffer> greenDraw = [device newBufferWithBytes:both[1] length:sizeof(vertices) options:MTLResourceStorageModeShared];
    [render setVertexBuffer:greenDraw offset:0 atIndex:0];
    [render setScissorRect:(MTLScissorRect){0,0,8,8}];
    [render drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:3 indexType:MTLIndexTypeUInt16 indexBuffer:ib16 indexBufferOffset:sizeof(uint16_t)];
    [greenDraw release]; [ib16 release];
    id<MTLBuffer> redDraw = [device newBufferWithBytes:both[0] length:sizeof(vertices) options:MTLResourceStorageModeShared];
    [render setVertexBuffer:redDraw offset:0 atIndex:0];
    [render setScissorRect:(MTLScissorRect){8,0,8,8}];
    [render drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:3 indexType:MTLIndexTypeUInt32 indexBuffer:ib32 indexBufferOffset:sizeof(uint32_t)];
    [redDraw release]; [ib32 release];
    if (getenv("RIOT_METAL_EARLY_RELEASE")) {
        // Drop the last public bindings and wrappers before submission. Earlier
        // draw resources must survive in the encoder's owning resolution maps.
        [render setVertexBuffer:nil offset:0 atIndex:0];
        [render setVertexBuffer:nil offset:0 atIndex:1];
        [render setVertexBuffer:nil offset:0 atIndex:2];
        [render setFragmentBuffer:nil offset:0 atIndex:0];
        [render setFragmentTexture:nil atIndex:0];
        [vb release]; vb = nil;
        [uniforms release]; uniforms = nil;
        [fragmentUniforms release]; fragmentUniforms = nil;
        [attributes release]; attributes = nil;
        [sampledDepth release]; sampledDepth = nil;
    }
    [render endEncoding];
    // Preserve the rendered attachment in a second encoder, then consume it
    // in a transfer encoder without a CPU/GPU wait between those accesses.
    pass.colorAttachments[colorSlot].loadAction = MTLLoadActionLoad;
    render = [commands renderCommandEncoderWithDescriptor:pass];
    [render endEncoding];
    id<MTLBuffer> output = [device newBufferWithLength:512 options:MTLResourceStorageModeShared];
    id<MTLBlitCommandEncoder> blit = [commands blitCommandEncoder];
    [blit copyFromTexture:color sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(16,8,1) toBuffer:output destinationOffset:0 destinationBytesPerRow:64 destinationBytesPerImage:512];
    [blit endEncoding]; [commands commit]; [commands waitUntilCompleted];
    unsigned char* pixels = (unsigned char*)output.contents;
    unsigned center = (4*16+8)*4;
    verifyBatch(pixels[center]>200 && pixels[center+1]==0 && pixels[center+2]==0, "translated shader draws red triangle");
    if (!getenv("RIOT_METAL_SKIP_COLOR_CHANGE_CHECK")) {
        unsigned left = (4*16+6)*4;
        verifyBatch(pixels[left]==0 && pixels[left+1]>200 && pixels[left+2]==0, "descriptor cache honors changed buffer offset");
    }
    [fragmentUniforms release];[attributes release];[sampledDepth release];[output release];[queue release];[vb release];[uniforms release];[color release];
    [pipeline release];[desc release];[vertex release];[fragment release];[library release];
    fprintf(stderr,"PASS: Metal shader data, AIR reflection, GPU triangle and encoder visibility\n");
}
