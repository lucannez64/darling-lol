/* CoreHaptics: Electron 43 binds the haptic pattern classes and their keys.
 *
 * Darling has no haptic engine, so patterns are accepted and never play. The
 * strings are the parameter identifiers callers use to build patterns. */
#import <Foundation/Foundation.h>

NSString *const CHHapticDynamicParameterIDHapticIntensityControl = @"CHHapticDynamicParameterIDHapticIntensityControl";
NSString *const CHHapticDynamicParameterIDHapticSharpnessControl = @"CHHapticDynamicParameterIDHapticSharpnessControl";
NSString *const CHHapticEventParameterIDHapticIntensity = @"CHHapticEventParameterIDHapticIntensity";
NSString *const CHHapticEventParameterIDHapticSharpness = @"CHHapticEventParameterIDHapticSharpness";
NSString *const CHHapticEventTypeHapticContinuous = @"CHHapticEventTypeHapticContinuous";
NSString *const CHHapticEventTypeHapticTransient = @"CHHapticEventTypeHapticTransient";

@interface CHHapticEventParameter : NSObject
{
	NSString *_parameterID;
	float _value;
}
- (instancetype)initWithParameterID:(NSString *)parameterID value:(float)value;
- (NSString *)parameterID;
- (float)value;
@end

@interface CHHapticDynamicParameter : NSObject
{
	NSString *_parameterID;
	float _value;
	double _relativeTime;
}
- (instancetype)initWithParameterID:(NSString *)parameterID value:(float)value relativeTime:(double)relativeTime;
- (NSString *)parameterID;
- (float)value;
@end

@interface CHHapticEvent : NSObject
{
	NSString *_eventType;
	NSArray *_parameters;
	double _relativeTime;
	double _duration;
}
- (instancetype)initWithEventType:(NSString *)eventType parameters:(NSArray *)parameters relativeTime:(double)relativeTime;
- (instancetype)initWithEventType:(NSString *)eventType parameters:(NSArray *)parameters relativeTime:(double)relativeTime duration:(double)duration;
- (NSString *)type;
- (NSArray *)eventParameters;
@end

@interface CHHapticPattern : NSObject
{
	NSArray *_events;
	NSArray *_parameters;
}
- (instancetype)initWithEvents:(NSArray *)events parameters:(NSArray *)parameters error:(NSError **)error;
- (NSArray *)events;
@end

@implementation CHHapticEventParameter
- (instancetype)initWithParameterID:(NSString *)parameterID value:(float)value
{
	if ((self = [super init]))
	{
		_parameterID = parameterID;
		_value = value;
	}
	return self;
}
- (NSString *)parameterID { return _parameterID; }
- (float)value { return _value; }
@end

@implementation CHHapticDynamicParameter
- (instancetype)initWithParameterID:(NSString *)parameterID value:(float)value relativeTime:(double)relativeTime
{
	if ((self = [super init]))
	{
		_parameterID = parameterID;
		_value = value;
		_relativeTime = relativeTime;
	}
	return self;
}
- (NSString *)parameterID { return _parameterID; }
- (float)value { return _value; }
@end

@implementation CHHapticEvent
- (instancetype)initWithEventType:(NSString *)eventType parameters:(NSArray *)parameters relativeTime:(double)relativeTime
{
	return [self initWithEventType:eventType parameters:parameters relativeTime:relativeTime duration:0];
}
- (instancetype)initWithEventType:(NSString *)eventType parameters:(NSArray *)parameters relativeTime:(double)relativeTime duration:(double)duration
{
	if ((self = [super init]))
	{
		_eventType = eventType;
		_parameters = parameters;
		_relativeTime = relativeTime;
		_duration = duration;
	}
	return self;
}
- (NSString *)type { return _eventType; }
- (NSArray *)eventParameters { return _parameters; }
@end

@implementation CHHapticPattern
- (instancetype)initWithEvents:(NSArray *)events parameters:(NSArray *)parameters error:(NSError **)error
{
	if ((self = [super init]))
	{
		_events = events;
		_parameters = parameters;
	}
	return self;
}
- (NSArray *)events { return _events; }
@end
