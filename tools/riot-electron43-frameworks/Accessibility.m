/* Accessibility: AXCustomContent is the one class Electron 43 binds.
 *
 * Chromium adds custom accessibility content to its controls, which VoiceOver
 * announces. There is no assistive client under Darling, so the object only has
 * to hold the label and value it is given. */
#import <Foundation/Foundation.h>

/* Chromium asks whether the user prefers non-blinking insertion indicators; with
 * no assistive client under Darling the answer is the default (NO) and the
 * notification is declared so a listener can register. */
BOOL AXPrefersNonBlinkingTextInsertionIndicator(void);
BOOL AXPrefersNonBlinkingTextInsertionIndicator(void) { return NO; }

NSString *const AXPrefersNonBlinkingTextInsertionIndicatorDidChangeNotification =
	@"AXPrefersNonBlinkingTextInsertionIndicatorDidChangeNotification";

@interface AXCustomContent : NSObject
{
	NSString *_label;
	NSString *_value;
}
+ (instancetype)customContentWithLabel:(NSString *)label value:(NSString *)value;
+ (instancetype)customContentWithAttributedLabel:(id)label value:(NSString *)value;
- (NSString *)label;
- (NSString *)value;
@end

@implementation AXCustomContent
+ (instancetype)customContentWithLabel:(NSString *)label value:(NSString *)value
{
	AXCustomContent *content = [AXCustomContent new];
	content->_label = label;
	content->_value = value;
	return content;
}
+ (instancetype)customContentWithAttributedLabel:(id)label value:(NSString *)value
{
	return [self customContentWithLabel:[label description] value:value];
}
- (NSString *)label { return _label; }
- (NSString *)value { return _value; }
@end
