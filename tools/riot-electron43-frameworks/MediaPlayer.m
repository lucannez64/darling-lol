/* MediaPlayer: Electron 43 binds the now-playing and remote-command classes.
 *
 * These back the macOS media-key and Control Center integration. Under Darling
 * there is no Now Playing service, so the classes hold their state locally:
 * a play/pause key does nothing instead of crashing the client. */
#import <Foundation/Foundation.h>

NSString *const MPMediaItemPropertyAlbumTitle = @"MPMediaItemPropertyAlbumTitle";
NSString *const MPMediaItemPropertyArtist = @"MPMediaItemPropertyArtist";
NSString *const MPMediaItemPropertyArtwork = @"MPMediaItemPropertyArtwork";
NSString *const MPMediaItemPropertyPlaybackDuration = @"MPMediaItemPropertyPlaybackDuration";
NSString *const MPMediaItemPropertyTitle = @"MPMediaItemPropertyTitle";
NSString *const MPNowPlayingInfoPropertyCurrentPlaybackDate = @"MPNowPlayingInfoPropertyCurrentPlaybackDate";
NSString *const MPNowPlayingInfoPropertyElapsedPlaybackTime = @"MPNowPlayingInfoPropertyElapsedPlaybackTime";
NSString *const MPNowPlayingInfoPropertyPlaybackRate = @"MPNowPlayingInfoPropertyPlaybackRate";

typedef NS_ENUM(NSInteger, MPRemoteCommandHandlerStatus) {
	MPRemoteCommandHandlerStatusSuccess = 0,
	MPRemoteCommandHandlerStatusNoSuchContent = 100,
	MPRemoteCommandHandlerStatusNoActionableNowPlayingItem = 110,
	MPRemoteCommandHandlerStatusDeviceNotFound = 120,
	MPRemoteCommandHandlerStatusCommandFailed = 200,
};

typedef NS_ENUM(NSInteger, MPNowPlayingPlaybackState) {
	MPNowPlayingPlaybackStateUnknown = 0,
	MPNowPlayingPlaybackStatePlaying,
	MPNowPlayingPlaybackStatePaused,
	MPNowPlayingPlaybackStateStopped,
};

@interface MPRemoteCommand : NSObject
{
	NSMutableArray *_targets;
}
- (id)addTargetWithHandler:(id)handler;
- (void)removeTarget:(id)target;
- (void)removeTarget:(id)target action:(SEL)action;
- (BOOL)isEnabled;
- (void)setEnabled:(BOOL)enabled;
@end

@interface MPRemoteCommandEvent : NSObject
@end

@interface MPRemoteCommandCenter : NSObject
{
	NSMutableDictionary *_commands;
}
+ (MPRemoteCommandCenter *)sharedCommandCenter;
- (MPRemoteCommand *)playCommand;
- (MPRemoteCommand *)pauseCommand;
- (MPRemoteCommand *)stopCommand;
- (MPRemoteCommand *)togglePlayPauseCommand;
- (MPRemoteCommand *)nextTrackCommand;
- (MPRemoteCommand *)previousTrackCommand;
- (MPRemoteCommand *)changePlaybackPositionCommand;
- (MPRemoteCommand *)changePlaybackRateCommand;
- (MPRemoteCommand *)seekForwardCommand;
- (MPRemoteCommand *)seekBackwardCommand;
- (MPRemoteCommand *)skipForwardCommand;
- (MPRemoteCommand *)skipBackwardCommand;
- (MPRemoteCommand *)ratingCommand;
- (MPRemoteCommand *)likeCommand;
- (MPRemoteCommand *)dislikeCommand;
- (MPRemoteCommand *)bookmarkCommand;
- (MPRemoteCommand *)languageOptionCommand;
@end

@interface MPNowPlayingInfoCenter : NSObject
{
	NSDictionary *_nowPlayingInfo;
	MPNowPlayingPlaybackState _playbackState;
}
+ (MPNowPlayingInfoCenter *)defaultCenter;
- (void)setNowPlayingInfo:(NSDictionary *)info;
- (NSDictionary *)nowPlayingInfo;
- (void)setPlaybackState:(MPNowPlayingPlaybackState)state;
- (MPNowPlayingPlaybackState)playbackState;
@end

@interface MPMediaItemArtwork : NSObject
{
	NSSize _boundsSize;
	id _requestHandler;
}
- (instancetype)initWithBoundsSize:(NSSize)boundsSize requestHandler:(id)handler;
/* AppKit-free declaration: Electron only ever asks for the artwork image. */
- (id)imageWithSize:(NSSize)size;
@end

@implementation MPRemoteCommand
- (instancetype)init
{
	if ((self = [super init]))
		_targets = [NSMutableArray new];
	return self;
}
- (id)addTargetWithHandler:(id)handler
{
    // A copied block is the opaque token returned to removeTarget:.
    if (!handler)
        return nil;
    id token = [handler copy];
    [_targets addObject:token];
    return token;
}
- (void)removeTarget:(id)target
{
	if (target)
		[_targets removeObjectIdenticalTo:target];
    else
        [_targets removeAllObjects];
}
- (void)removeTarget:(id)target action:(SEL)action { [self removeTarget:target]; }
- (BOOL)isEnabled { return YES; }
- (void)setEnabled:(BOOL)enabled { }
@end

@implementation MPRemoteCommandEvent
@end

@implementation MPRemoteCommandCenter
+ (MPRemoteCommandCenter *)sharedCommandCenter
{
	static MPRemoteCommandCenter *center;
	if (!center)
		center = [MPRemoteCommandCenter new];
	return center;
}
- (instancetype)init
{
	if ((self = [super init]))
		_commands = [NSMutableDictionary new];
	return self;
}
- (MPRemoteCommand *)commandFor:(NSString *)name
{
	MPRemoteCommand *command = [_commands objectForKey:name];
	if (!command)
	{
		command = [MPRemoteCommand new];
		[_commands setObject:command forKey:name];
	}
	return command;
}
- (MPRemoteCommand *)playCommand { return [self commandFor:@"play"]; }
- (MPRemoteCommand *)pauseCommand { return [self commandFor:@"pause"]; }
- (MPRemoteCommand *)stopCommand { return [self commandFor:@"stop"]; }
- (MPRemoteCommand *)togglePlayPauseCommand { return [self commandFor:@"togglePlayPause"]; }
- (MPRemoteCommand *)nextTrackCommand { return [self commandFor:@"nextTrack"]; }
- (MPRemoteCommand *)previousTrackCommand { return [self commandFor:@"previousTrack"]; }
- (MPRemoteCommand *)changePlaybackPositionCommand { return [self commandFor:@"changePlaybackPosition"]; }
- (MPRemoteCommand *)changePlaybackRateCommand { return [self commandFor:@"changePlaybackRate"]; }
- (MPRemoteCommand *)seekForwardCommand { return [self commandFor:@"seekForward"]; }
- (MPRemoteCommand *)seekBackwardCommand { return [self commandFor:@"seekBackward"]; }
- (MPRemoteCommand *)skipForwardCommand { return [self commandFor:@"skipForward"]; }
- (MPRemoteCommand *)skipBackwardCommand { return [self commandFor:@"skipBackward"]; }
- (MPRemoteCommand *)ratingCommand { return [self commandFor:@"rating"]; }
- (MPRemoteCommand *)likeCommand { return [self commandFor:@"like"]; }
- (MPRemoteCommand *)dislikeCommand { return [self commandFor:@"dislike"]; }
- (MPRemoteCommand *)bookmarkCommand { return [self commandFor:@"bookmark"]; }
- (MPRemoteCommand *)languageOptionCommand { return [self commandFor:@"languageOption"]; }
@end

@implementation MPNowPlayingInfoCenter
+ (MPNowPlayingInfoCenter *)defaultCenter
{
	static MPNowPlayingInfoCenter *center;
	if (!center)
		center = [MPNowPlayingInfoCenter new];
	return center;
}
- (void)setNowPlayingInfo:(NSDictionary *)info { _nowPlayingInfo = info; }
- (NSDictionary *)nowPlayingInfo { return _nowPlayingInfo; }
- (void)setPlaybackState:(MPNowPlayingPlaybackState)state { _playbackState = state; }
- (MPNowPlayingPlaybackState)playbackState { return _playbackState; }
@end

@implementation MPMediaItemArtwork
- (instancetype)initWithBoundsSize:(NSSize)boundsSize requestHandler:(id)handler
{
	if ((self = [super init]))
	{
		_boundsSize = boundsSize;
		_requestHandler = [handler copy];
	}
	return self;
}
- (id)imageWithSize:(NSSize)size { return nil; }
@end
