#import <AppKit/NSEvent.h>
#import <CoreGraphics/CGEvent.h>
#import <Foundation/Foundation.h>
#include <assert.h>
#include <stdio.h>

@interface NSEvent (ScrollProbe)
+ (id)mouseEventWithType:(NSEventType)type location:(NSPoint)point
    modifierFlags:(NSUInteger)flags window:(id)window clickCount:(NSInteger)count
    deltaX:(CGFloat)x deltaY:(CGFloat)y;
- (NSUInteger)phase;
- (NSUInteger)momentumPhase;
- (BOOL)hasPreciseScrollingDeltas;
- (CGFloat)scrollingDeltaX;
- (CGFloat)scrollingDeltaY;
- (CGEventRef)CGEvent;
@end

int main(void) {
    NSAutoreleasePool *pool=[NSAutoreleasePool new];
    CGFloat axes[][2]={{0,1},{0,-1},{1,0},{-1,0}};
    for(unsigned i=0;i<4;++i) {
        NSEvent *event=[NSEvent mouseEventWithType:NSScrollWheel
            location:NSMakePoint(12,24) modifierFlags:0 window:nil
            clickCount:0 deltaX:axes[i][0] deltaY:axes[i][1]];
        assert([event respondsToSelector:@selector(CGEvent)]);
        assert([event respondsToSelector:@selector(phase)]);
        assert([event phase]==0 && [event momentumPhase]==0);
        assert(![event hasPreciseScrollingDeltas]);
        assert([event scrollingDeltaX]==axes[i][0]);
        assert([event scrollingDeltaY]==axes[i][1]);
        assert([event timestamp] > 0.0 && [event timestamp] < 315360000.0);
        CGEventRef cg=[event CGEvent];
        assert(cg && CGEventGetType(cg)==kCGEventScrollWheel);
        assert(CGEventGetIntegerValueField(cg,kCGScrollWheelEventIsContinuous)==0);
        assert(CGEventGetIntegerValueField(cg,kCGScrollWheelEventDeltaAxis1)==axes[i][1]);
        assert(CGEventGetIntegerValueField(cg,kCGScrollWheelEventDeltaAxis2)==axes[i][0]);
        assert(CGEventGetDoubleValueField(cg,kCGScrollWheelEventFixedPtDeltaAxis1)==axes[i][1]);
        assert(CGEventGetDoubleValueField(cg,kCGScrollWheelEventFixedPtDeltaAxis2)==axes[i][0]);
    }
    puts("SCROLL_EVENT vertical=pass horizontal=pass phases=none cg_notches=pass");
    [pool drain];
    return 0;
}
