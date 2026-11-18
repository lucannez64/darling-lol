/* Standalone software Keychain probe. All items and messages are synthetic. */
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>

#define S(symbol) ((NSString *)(symbol))

static void record(NSMutableDictionary *cases, NSString *name, BOOL passed, NSInteger status)
{
    cases[name] = @{@"passed": @(passed), @"status": @(status)};
}

static void checkStatus(NSMutableDictionary *cases, NSString *name,
                        OSStatus actual, OSStatus expected)
{
    record(cases, name, actual == expected, actual);
}

static NSData *data(NSString *string)
{
    return [string dataUsingEncoding:NSUTF8StringEncoding];
}

static void exerciseKey(NSMutableDictionary *cases, NSMutableDictionary *keyIDs,
                        NSString *name, NSData *tag,
                        CFStringRef accessibility, BOOL create)
{
    NSDictionary *attributes = @{
        S(kSecClass): S(kSecClassKey), S(kSecAttrKeyType): S(kSecAttrKeyTypeECSECPrimeRandom),
        S(kSecAttrKeySizeInBits): @256, S(kSecAttrApplicationTag): tag,
        S(kSecAttrAccessible): (id)accessibility, S(kSecAttrCanSign): @YES,
        S(kSecAttrCanVerify): @YES, S(kSecUseDataProtectionKeychain): @YES
    };
    NSMutableDictionary *lookup = [NSMutableDictionary dictionaryWithDictionary:attributes];
    lookup[S(kSecAttrKeyClass)] = S(kSecAttrKeyClassPrivate);
    CFErrorRef error = NULL;
    if (create) {
        SecKeyRef key = SecKeyCreateRandomKey((CFDictionaryRef)attributes, &error);
        CFDictionaryRef actual = key ? SecKeyCopyAttributes(key) : NULL;
        BOOL software = actual && !CFDictionaryContainsKey(actual, kSecAttrTokenID) &&
            [((NSDictionary *)actual)[S(kSecAttrKeyType)] isEqual:S(kSecAttrKeyTypeECSECPrimeRandom)];
        record(cases, [name stringByAppendingString:@".generate"], software,
               error ? CFErrorGetCode(error) : 0);
        if (key) {
            CFErrorRef importError = NULL;
            CFDataRef privateBytes = SecKeyCopyExternalRepresentation(key, &importError);
            SecKeyRef restored = privateBytes
                ? SecKeyCreateWithData(privateBytes, (CFDictionaryRef)lookup, &importError) : NULL;
            record(cases, [name stringByAppendingString:@".private_export_import"],
                   privateBytes && CFDataGetLength(privateBytes) == 97 && restored,
                   importError ? CFErrorGetCode(importError) : 0);
            if (importError) {
                CFStringRef description = CFErrorCopyDescription(importError);
                fprintf(stderr, "Private key round-trip: %s\n", [(NSString *)description UTF8String]);
                CFRelease(description);
                CFRelease(importError);
            }
            if (restored) CFRelease(restored);
            if (privateBytes) CFRelease(privateBytes);
        }
        NSMutableDictionary *add = [NSMutableDictionary dictionaryWithDictionary:lookup];
        if (key) add[S(kSecValueRef)] = (id)key;
        checkStatus(cases, [name stringByAppendingString:@".add"],
                    SecItemAdd((CFDictionaryRef)add, NULL), errSecSuccess);
        if (actual) CFRelease(actual);
        if (key) CFRelease(key);
        if (error) CFRelease(error);
        error = NULL;
    }

    lookup[S(kSecReturnRef)] = @YES;
    CFTypeRef fetched = NULL;
    OSStatus status = SecItemCopyMatching((CFDictionaryRef)lookup, &fetched);
    SecKeyRef privateKey = fetched && CFGetTypeID(fetched) == SecKeyGetTypeID()
                          ? (SecKeyRef)fetched : NULL;
    record(cases, [name stringByAppendingString:@".fetch"],
           status == errSecSuccess && privateKey, status);

    NSData *message = data(@"Darling Keychain compatibility");
    SecKeyAlgorithm algorithm = kSecKeyAlgorithmECDSASignatureMessageX962SHA256;
    CFDataRef signature = privateKey
        ? SecKeyCreateSignature(privateKey, algorithm, (CFDataRef)message, &error) : NULL;
    record(cases, [name stringByAppendingString:@".sign"], signature != NULL,
           error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;

    SecKeyRef publicKey = privateKey ? SecKeyCopyPublicKey(privateKey) : NULL;
    BOOL verified = publicKey && signature && SecKeyVerifySignature(
        publicKey, algorithm, (CFDataRef)message, signature, &error);
    record(cases, [name stringByAppendingString:@".verify"], verified,
           error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;

    CFDataRef bytes = publicKey ? SecKeyCopyExternalRepresentation(publicKey, &error) : NULL;
    if (privateKey) {
        NSDictionary *attrs = [(NSDictionary *)SecKeyCopyAttributes(privateKey) autorelease];
        NSData *applicationLabel = attrs[S(kSecAttrApplicationLabel)];
        if ([applicationLabel length] == 20)
            keyIDs[name] = [applicationLabel base64EncodedStringWithOptions:0];
    }
    NSDictionary *publicAttributes = @{
        S(kSecAttrKeyType): S(kSecAttrKeyTypeECSECPrimeRandom),
        S(kSecAttrKeyClass): S(kSecAttrKeyClassPublic), S(kSecAttrKeySizeInBits): @256
    };
    SecKeyRef imported = bytes
        ? SecKeyCreateWithData(bytes, (CFDictionaryRef)publicAttributes, &error) : NULL;
    verified = imported && signature && SecKeyVerifySignature(
        imported, algorithm, (CFDataRef)message, signature, &error);
    record(cases, [name stringByAppendingString:@".export_import_verify"],
           bytes && CFDataGetLength(bytes) == 65 && verified, error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;
    BOOL rejected = imported && signature && !SecKeyVerifySignature(
        imported, algorithm, (CFDataRef)data(@"different message"), signature, &error);
    record(cases, [name stringByAppendingString:@".reject_wrong_message"], rejected,
           error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    if (imported) CFRelease(imported);
    if (bytes) CFRelease(bytes);
    if (publicKey) CFRelease(publicKey);
    if (signature) CFRelease(signature);
    if (fetched) CFRelease(fetched);

    if (!create) {
        [lookup removeObjectForKey:S(kSecReturnRef)];
        checkStatus(cases, [name stringByAppendingString:@".delete"],
                    SecItemDelete((CFDictionaryRef)lookup), errSecSuccess);
        checkStatus(cases, [name stringByAppendingString:@".missing"],
                    SecItemCopyMatching((CFDictionaryRef)lookup, NULL), errSecItemNotFound);
    }
}

static void fetchPassword(NSMutableDictionary *cases, NSString *name, NSDictionary *query,
                          NSData *expected)
{
    CFTypeRef result = NULL;
    OSStatus status = SecItemCopyMatching((CFDictionaryRef)query, &result);
    record(cases, name, status == errSecSuccess && result && CFEqual(result, expected), status);
    if (result) CFRelease(result);
}

static void exercisePassword(NSMutableDictionary *cases, NSString *account, BOOL create)
{
    NSDictionary *identity = @{
        S(kSecClass): S(kSecClassGenericPassword), S(kSecAttrService): @"org.darling.keychain-compat",
        S(kSecAttrAccount): account, S(kSecUseDataProtectionKeychain): @YES
    };
    NSMutableDictionary *query = [NSMutableDictionary dictionaryWithDictionary:identity];
    query[S(kSecReturnData)] = @YES;
    NSData *updated = data(@"synthetic updated");
    if (create) {
        NSData *initial = data(@"synthetic initial");
        NSMutableDictionary *add = [NSMutableDictionary dictionaryWithDictionary:identity];
        add[S(kSecValueData)] = initial;
        checkStatus(cases, @"password.add", SecItemAdd((CFDictionaryRef)add, NULL), errSecSuccess);
        checkStatus(cases, @"password.duplicate", SecItemAdd((CFDictionaryRef)add, NULL),
                    errSecDuplicateItem);
        fetchPassword(cases, @"password.fetch", query, initial);
        checkStatus(cases, @"password.update", SecItemUpdate((CFDictionaryRef)identity,
                    (CFDictionaryRef)@{S(kSecValueData): updated}), errSecSuccess);
    }
    fetchPassword(cases, @"password.fetch_updated", query, updated);
    if (!create) {
        checkStatus(cases, @"password.delete", SecItemDelete((CFDictionaryRef)identity), errSecSuccess);
        checkStatus(cases, @"password.missing", SecItemCopyMatching((CFDictionaryRef)query, NULL),
                    errSecItemNotFound);
    }
}

static void exerciseSecureEnclave(NSMutableDictionary *cases)
{
    NSDictionary *attributes = @{
        S(kSecAttrKeyType): S(kSecAttrKeyTypeECSECPrimeRandom), S(kSecAttrKeySizeInBits): @256,
        S(kSecAttrTokenID): S(kSecAttrTokenIDSecureEnclave)
    };
    CFErrorRef error = NULL;
    SecKeyRef key = SecKeyCreateRandomKey((CFDictionaryRef)attributes, &error);
    NSInteger code = error ? CFErrorGetCode(error) : 0;
    record(cases, @"secure_enclave.unavailable",
           !key && (code == errSecNotAvailable || code == errSecUnimplemented), code);
    if (key) CFRelease(key);
    if (error) CFRelease(error);
}

int main(int argc, const char **argv)
{
    if (argc != 3 || (strcmp(argv[1], "create") && strcmp(argv[1], "reopen"))) {
        fprintf(stderr, "usage: keychain_compat create|reopen unique-id\n");
        return 2;
    }
    @autoreleasepool {
        BOOL create = !strcmp(argv[1], "create");
        NSString *identifier = [NSString stringWithUTF8String:argv[2]];
        NSMutableDictionary *cases = [NSMutableDictionary dictionary];
        NSMutableDictionary *keyIDs = [NSMutableDictionary dictionary];
        exerciseKey(cases, keyIDs, @"after_first_unlock", data([identifier stringByAppendingString:@".first"]),
                    kSecAttrAccessibleAfterFirstUnlock, create);
        exerciseKey(cases, keyIDs, @"when_unlocked", data([identifier stringByAppendingString:@".unlocked"]),
                    kSecAttrAccessibleWhenUnlocked, create);
        exercisePassword(cases, identifier, create);
        if (create) exerciseSecureEnclave(cases);

        unsigned failures = 0;
        for (NSDictionary *result in [cases allValues]) {
            if (![result[@"passed"] boolValue]) failures++;
        }
        Dl_info image;
        NSString *securityImage = dladdr((const void *)SecItemAdd, &image)
            ? [NSString stringWithUTF8String:image.dli_fname] : @"";
        NSDictionary *report = @{@"schema": @1, @"probe": @"keychain_compat",
            @"mode": [NSString stringWithUTF8String:argv[1]], @"pid": @(getpid()),
            @"security_image": securityImage, @"key_ids": keyIDs,
            @"cases": cases, @"failures": @(failures)};
        NSData *output = [NSJSONSerialization dataWithJSONObject:report options:0 error:NULL];
        if (!output) return 2;
        fwrite([output bytes], 1, [output length], stdout);
        putchar('\n');
        return failures ? 1 : 0;
    }
}
