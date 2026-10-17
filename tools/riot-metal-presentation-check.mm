#import <AppKit/AppKit.h>
#import <QuartzCore/CAMetalLayer.h>
#import <QuartzCore/CAMetalDrawable.h>
#import <Metal/Metal.h>
#import <QuartzCore/CALayerContext.h>
#import "CALayerInternal.h"
#import <OpenGL/OpenGL.h>
#import <OpenGL/gl.h>
#include <stdio.h>
#include <stdlib.h>

static void verifyPresentation(bool value, const char* message) {
    if (!value) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
}
static void pump(NSApplication* app) {
    NSDate* end = [NSDate dateWithTimeIntervalSinceNow:0.04];
    while ([end timeIntervalSinceNow] > 0) {
        NSEvent* event = [app nextEventMatchingMask:NSAnyEventMask untilDate:[NSDate distantPast]
                                            inMode:NSDefaultRunLoopMode dequeue:YES];
        if (event) [app sendEvent:event];
        [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.005]];
    }
}
int main() {
    @autoreleasepool {
        NSApplication* app = [NSApplication sharedApplication];
        const bool captureTest = getenv("RIOT_FRAME_CAPTURE_TEST") != nullptr;
        if (captureTest) {
            FILE* flag = fopen("/tmp/metal-frame-interval-capture", "a");
            verifyPresentation(flag != nullptr, "enable bounded capture");
            fclose(flag);
            remove("/tmp/metal-frame-interval-repeat");
            remove("/tmp/metal-present-intervals-8.csv");
        }
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        verifyPresentation(device != nil, "device");
        CAMetalLayer* layer = [CAMetalLayer layer];
        layer.device = device;
        layer.drawableSize = CGSizeMake(160,96);
        layer.frame = CGRectMake(0,0,160,96);
        NSView* view = [[NSView alloc] initWithFrame:NSMakeRect(0,0,160,96)];
        view.wantsLayer = YES;
        const bool nested = getenv("RIOT_PRESENTATION_NESTED") != nullptr;
        if (nested) {
            CALayer* root = [CALayer layer];
            root.frame = layer.frame;
            CALayer* container = [CALayer layer];
            container.frame = layer.frame;
            [container addSublayer:layer];
            [root addSublayer:container];
            view.layer = root;
        } else {
            view.layer = layer;
        }
        NSWindow* window = [[NSWindow alloc] initWithContentRect:NSMakeRect(50,50,160,96)
          styleMask:NSTitledWindowMask backing:NSBackingStoreBuffered defer:NO];
        window.title = @"Metal presentation check";
        window.contentView = view;
        [window makeKeyAndOrderFront:nil];
        pump(app);
        id<MTLCommandQueue> queue = [device newCommandQueue];
        // RIOT_PRESENTATION_HOLD=<seconds> keeps presenting so the window's pixels can be captured from the host.
        const unsigned holdFrames = getenv("RIOT_PRESENTATION_HOLD") ? (unsigned) atoi(getenv("RIOT_PRESENTATION_HOLD")) * 20u : 0u;
        const unsigned frameCount = captureTest ? 130u : getenv("RIOT_PRESENTATION_STRESS") ? 120u : holdFrames ? holdFrames : 10u;
        for (unsigned frame = 0; frame < frameCount; ++frame) {
            if (captureTest && frame == 80) {
                FILE* flag = fopen("/tmp/metal-frame-interval-repeat", "a");
                verifyPresentation(flag != nullptr, "request second bounded capture");
                fclose(flag);
            }
            @autoreleasepool {
                id<CAMetalDrawable> drawable = [layer nextDrawable];
                verifyPresentation(drawable != nil, "drawable after late window/context attachment");
                verifyPresentation(drawable.texture.width == 160 && drawable.texture.height == 96, "drawable dimensions");
                MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
                pass.colorAttachments[0].texture = drawable.texture;
                pass.colorAttachments[0].loadAction = MTLLoadActionClear;
                pass.colorAttachments[0].storeAction = MTLStoreActionStore;
                pass.colorAttachments[0].clearColor = MTLClearColorMake(1,0,0,1);
                id<MTLCommandBuffer> commands = [queue commandBuffer];
                id<MTLRenderCommandEncoder> encoder = [commands renderCommandEncoderWithDescriptor:pass];
                [encoder endEncoding];
                [commands presentDrawable:drawable];
                [commands commit];
                [commands waitUntilCompleted];
                pump(app);
            }
        }
        CGLContextObj previous = CGLGetCurrentContext();
        CGLSetCurrentContext([(CALayerContext*)[layer _context] glContext]);
        glBindTexture(GL_TEXTURE_2D, [[layer _textureId] unsignedIntValue]);
        unsigned char pixels[160 * 96 * 4] = {};
        glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
        verifyPresentation(glGetError() == GL_NO_ERROR, "presentation texture readback");
        verifyPresentation(pixels[0] > 250 && pixels[1] < 5 && pixels[2] < 5 && pixels[3] > 250,
                           "Vulkan clear reaches the OpenGL presentation texture");
        CGLSetCurrentContext(previous);
        if (captureTest) {
            FILE* capture = fopen("/tmp/metal-present-intervals-8.csv", "r");
            verifyPresentation(capture != nullptr, "second capture without restarting the process");
            char line[256]; unsigned rows = 0;
            while (fgets(line, sizeof(line), capture)) ++rows;
            fclose(capture);
            verifyPresentation(rows == 9, "second capture has header and eight samples");
            fprintf(stderr, "PASS: bounded presentation capture rearmed without restarting\n");
        }
        fprintf(stderr, "PASS: Vulkan-to-OpenGL presentation pixels\n");
        fprintf(stderr, "PASS: %s layer, late context attachment and %u Metal drawable presentations\n",
                nested ? "nested" : "root", frameCount);
        [window orderOut:nil];
        [queue release]; [view release]; [window release]; [device release];
    }
    fprintf(stderr, "PASS: presentation cleanup\n");
    return 0;
}
