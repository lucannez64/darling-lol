/* CoreML: Electron 43 binds five classes for its machine-learning bridge.
 *
 * The Riot Client does not run a model to show its window, and Darling has no
 * Neural Engine or GPU compute backend to run one on. The classes exist so the
 * image binds; creating a model fails with an error instead of returning a
 * half-initialized object. */
#import <Foundation/Foundation.h>

extern NSString *const MLModelErrorDomain;
NSString *const MLModelErrorDomain = @"MLModelErrorDomain";

typedef NS_ENUM(NSInteger, MLModelError) {
	MLModelErrorGeneric = 0,
	MLModelErrorFeatureType = 1,
	MLModelErrorIO = 3,
};

@interface MLModelConfiguration : NSObject
{
	NSInteger _computeUnits;
}
- (NSInteger)computeUnits;
- (void)setComputeUnits:(NSInteger)units;
@end

@interface MLPredictionOptions : NSObject
@end

@interface MLMultiArray : NSObject
- (instancetype)initWithShape:(NSArray *)shape dataType:(NSInteger)dataType error:(NSError **)error;
- (NSArray *)shape;
- (NSInteger)count;
@end

@interface MLFeatureValue : NSObject
{
	id _value;
}
+ (instancetype)featureValueWithString:(NSString *)value;
+ (instancetype)featureValueWithInt64:(long long)value;
+ (instancetype)featureValueWithDouble:(double)value;
+ (instancetype)featureValueWithDictionary:(NSDictionary *)value error:(NSError **)error;
- (NSString *)stringValue;
- (long long)int64Value;
- (double)doubleValue;
@end

@interface MLModel : NSObject
+ (instancetype)modelWithContentsOfURL:(NSURL *)url error:(NSError **)error;
+ (instancetype)modelWithContentsOfURL:(NSURL *)url configuration:(MLModelConfiguration *)configuration error:(NSError **)error;
- (NSArray *)modelDescription;
@end

/* Compute devices and the plan that assigns models to them. Darling has no
 * Neural Engine and no GPU compute backend, so a plan is empty: the clients that
 * inspect it find no usable device rather than a crash. */
@interface MLCPUComputeDevice : NSObject
@end

@interface MLGPUComputeDevice : NSObject
@end

@interface MLNeuralEngineComputeDevice : NSObject
@end

@interface MLComputePlan : NSObject
+ (instancetype)loadContentsOfURL:(NSURL *)url configuration:(MLModelConfiguration *)configuration error:(NSError **)error;
- (id)computeDeviceUsageForMLProgramOperation:(id)operation;
@end

@implementation MLCPUComputeDevice
@end

@implementation MLGPUComputeDevice
@end

@implementation MLNeuralEngineComputeDevice
@end

@implementation MLComputePlan
+ (instancetype)loadContentsOfURL:(NSURL *)url configuration:(MLModelConfiguration *)configuration error:(NSError **)error
{
	if (error)
		*error = [NSError errorWithDomain:MLModelErrorDomain code:MLModelErrorGeneric
		                         userInfo:[NSDictionary dictionaryWithObject:@"CoreML is not available under Darling"
		                                                              forKey:NSLocalizedDescriptionKey]];
	return nil;
}
- (id)computeDeviceUsageForMLProgramOperation:(id)operation { return nil; }
@end

@implementation MLModelConfiguration
- (NSInteger)computeUnits { return _computeUnits; }
- (void)setComputeUnits:(NSInteger)units { _computeUnits = units; }
@end

@implementation MLPredictionOptions
@end

@implementation MLMultiArray
- (instancetype)initWithShape:(NSArray *)shape dataType:(NSInteger)dataType error:(NSError **)error
{
	if (error)
		*error = [NSError errorWithDomain:MLModelErrorDomain code:MLModelErrorFeatureType userInfo:nil];
	return nil;
}
- (NSArray *)shape { return @[]; }
- (NSInteger)count { return 0; }
@end

@implementation MLFeatureValue
+ (instancetype)featureValueWithString:(NSString *)value
{
	MLFeatureValue *feature = [MLFeatureValue new];
	feature->_value = value;
	return feature;
}
+ (instancetype)featureValueWithInt64:(long long)value
{
	MLFeatureValue *feature = [MLFeatureValue new];
	feature->_value = [NSNumber numberWithLongLong:value];
	return feature;
}
+ (instancetype)featureValueWithDouble:(double)value
{
	MLFeatureValue *feature = [MLFeatureValue new];
	feature->_value = [NSNumber numberWithDouble:value];
	return feature;
}
+ (instancetype)featureValueWithDictionary:(NSDictionary *)value error:(NSError **)error
{
	if (error)
		*error = [NSError errorWithDomain:MLModelErrorDomain code:MLModelErrorFeatureType userInfo:nil];
	return nil;
}
- (NSString *)stringValue { return [_value isKindOfClass:[NSString class]] ? _value : nil; }
- (long long)int64Value { return [_value respondsToSelector:@selector(longLongValue)] ? [_value longLongValue] : 0; }
- (double)doubleValue { return [_value respondsToSelector:@selector(doubleValue)] ? [_value doubleValue] : 0.0; }
@end

@implementation MLModel
+ (instancetype)modelWithContentsOfURL:(NSURL *)url error:(NSError **)error
{
	return [self modelWithContentsOfURL:url configuration:nil error:error];
}
+ (instancetype)modelWithContentsOfURL:(NSURL *)url configuration:(MLModelConfiguration *)configuration error:(NSError **)error
{
	if (error)
		*error = [NSError errorWithDomain:MLModelErrorDomain code:MLModelErrorGeneric
		                         userInfo:[NSDictionary dictionaryWithObject:@"CoreML is not available under Darling"
		                                                              forKey:NSLocalizedDescriptionKey]];
	return nil;
}
- (NSArray *)modelDescription { return nil; }
@end
