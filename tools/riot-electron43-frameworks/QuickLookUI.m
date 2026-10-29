/* QuickLookUI: only QLPreviewPanel is referenced by Electron 43 under Darling.
 *
 * The panel is part of the Quick Look preview of a downloaded file. Nothing in
 * the Riot Client opens one while it is signing in, so this stub exists to let
 * dyld bind the class and to keep the panel harmless if it is ever shown: an
 * empty panel rather than a crash. */
#import <AppKit/AppKit.h>

@interface QLPreviewPanel : NSPanel
{
	id _qlDataSource;
	id _qlDelegate;
}
+ (QLPreviewPanel *)sharedPreviewPanel;
+ (BOOL)sharedPreviewPanelExists;
- (void)setDataSource:(id)dataSource;
- (id)dataSource;
- (void)setDelegate:(id)delegate;
- (id)delegate;
- (void)reloadData;
- (BOOL)isOpen;
- (void)makeKeyAndOrderFront:(id)sender;
- (void)close;
@end

static QLPreviewPanel *panel;

@implementation QLPreviewPanel

+ (QLPreviewPanel *)sharedPreviewPanel
{
	if (!panel)
		panel = [[QLPreviewPanel alloc] initWithContentRect:NSMakeRect(0, 0, 640, 480)
		                                         styleMask:NSWindowStyleMaskTitled
		                                           backing:NSBackingStoreBuffered
		                                             defer:YES];
	return panel;
}

+ (BOOL)sharedPreviewPanelExists
{
	return panel != nil;
}

- (void)setDataSource:(id)dataSource { _qlDataSource = dataSource; }
- (id)dataSource { return _qlDataSource; }
- (void)setDelegate:(id)delegate { _qlDelegate = delegate; }
- (id)delegate { return _qlDelegate; }
- (void)reloadData { }
- (BOOL)isOpen { return [self isVisible]; }
- (void)makeKeyAndOrderFront:(id)sender { }
- (void)close { }

@end
