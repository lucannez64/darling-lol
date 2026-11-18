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

/* Query forms real clients use: permanent generation, access control, access groups. */
static void exerciseQueryForms(NSMutableDictionary *cases, NSString *identifier)
{
    NSData *tag = data([identifier stringByAppendingString:@".permanent"]);
    NSData *aclTag = data([identifier stringByAppendingString:@".acl"]);
    NSDictionary *base = @{S(kSecAttrKeyType): S(kSecAttrKeyTypeECSECPrimeRandom),
                           S(kSecAttrKeySizeInBits): @256, S(kSecUseDataProtectionKeychain): @YES};
    NSMutableDictionary *generate = [NSMutableDictionary dictionaryWithDictionary:base];
    generate[S(kSecPrivateKeyAttrs)] = @{S(kSecAttrIsPermanent): @YES, S(kSecAttrApplicationTag): tag};
    CFErrorRef error = NULL;
    SecKeyRef key = SecKeyCreateRandomKey((CFDictionaryRef)generate, &error);
    record(cases, @"forms.permanent_generate", key != NULL, error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;
    if (key) CFRelease(key);

    SecAccessControlRef control = SecAccessControlCreateWithFlags(
        NULL, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly, 0, NULL);
    if (control) {
        generate[S(kSecPrivateKeyAttrs)] = @{S(kSecAttrIsPermanent): @YES,
            S(kSecAttrApplicationTag): aclTag, S(kSecAttrAccessControl): (id)control};
        key = SecKeyCreateRandomKey((CFDictionaryRef)generate, &error);
        CFRelease(control);
    } else {
        key = NULL;
    }
    record(cases, @"forms.access_control_generate", key != NULL, error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;
    if (key) CFRelease(key);

    /* An application-password requirement cannot be enforced, so the store must not take it. */
    control = SecAccessControlCreateWithFlags(NULL, kSecAttrAccessibleWhenUnlocked,
                                              kSecAccessControlApplicationPassword, NULL);
    key = NULL;
    if (control) {
        generate[S(kSecPrivateKeyAttrs)] = @{S(kSecAttrIsPermanent): @YES,
            S(kSecAttrApplicationTag): data([identifier stringByAppendingString:@".password-acl"]),
            S(kSecAttrAccessControl): (id)control};
        key = SecKeyCreateRandomKey((CFDictionaryRef)generate, &error);
        CFRelease(control);
    }
    record(cases, @"forms.password_acl_declined", control && !key,
           error ? CFErrorGetCode(error) : 0);
    if (error) CFRelease(error);
    error = NULL;
    if (key) CFRelease(key);

    /* A pair whose private half cannot be stored must not leave its public half behind. */
    NSData *halfTag = data([identifier stringByAppendingString:@".half"]);
    control = SecAccessControlCreateWithFlags(NULL, kSecAttrAccessibleWhenUnlocked,
                                              kSecAccessControlApplicationPassword, NULL);
    key = NULL;
    if (control) {
        NSMutableDictionary *pair = [NSMutableDictionary dictionaryWithDictionary:base];
        pair[S(kSecAttrIsPermanent)] = @YES;
        pair[S(kSecAttrApplicationTag)] = halfTag;
        pair[S(kSecPrivateKeyAttrs)] = @{S(kSecAttrAccessControl): (id)control};
        key = SecKeyCreateRandomKey((CFDictionaryRef)pair, &error);
        CFRelease(control);
    }
    NSDictionary *publicHalf = @{S(kSecClass): S(kSecClassKey), S(kSecAttrApplicationTag): halfTag,
        S(kSecAttrKeyClass): S(kSecAttrKeyClassPublic), S(kSecUseDataProtectionKeychain): @YES};
    OSStatus halfStatus = SecItemCopyMatching((CFDictionaryRef)publicHalf, NULL);
    record(cases, @"forms.failed_pair_leaves_nothing",
           control && !key && halfStatus == errSecItemNotFound, halfStatus);
    if (error) CFRelease(error);
    if (key) CFRelease(key);

    for (NSData *wanted in @[tag, aclTag]) {
        NSString *name = wanted == tag ? @"forms.permanent" : @"forms.access_control";
        NSDictionary *query = @{S(kSecClass): S(kSecClassKey), S(kSecAttrApplicationTag): wanted,
            S(kSecAttrKeyClass): S(kSecAttrKeyClassPrivate), S(kSecUseDataProtectionKeychain): @YES};
        NSMutableDictionary *fetch = [NSMutableDictionary dictionaryWithDictionary:query];
        fetch[S(kSecReturnRef)] = @YES;
        fetch[S(kSecAttrSynchronizable)] = S(kSecAttrSynchronizableAny);
        CFTypeRef fetched = NULL;
        OSStatus status = SecItemCopyMatching((CFDictionaryRef)fetch, &fetched);
        record(cases, [name stringByAppendingString:@"_fetch"],
               status == errSecSuccess && fetched && CFGetTypeID(fetched) == SecKeyGetTypeID(), status);
        if (fetched) CFRelease(fetched);
        checkStatus(cases, [name stringByAppendingString:@"_delete"],
                    SecItemDelete((CFDictionaryRef)query), errSecSuccess);
    }

    NSDictionary *identity = @{S(kSecClass): S(kSecClassGenericPassword),
        S(kSecAttrService): @"org.darling.keychain-compat.forms", S(kSecAttrAccount): identifier,
        S(kSecAttrAccessGroup): @"org.darling.keychain-compat", S(kSecUseDataProtectionKeychain): @YES};
    NSMutableDictionary *add = [NSMutableDictionary dictionaryWithDictionary:identity];
    add[S(kSecValueData)] = tag;
    add[S(kSecAttrDescription)] = @"synthetic";
    add[S(kSecAttrAccessible)] = S(kSecAttrAccessibleWhenUnlockedThisDeviceOnly);
    checkStatus(cases, @"forms.password_add", SecItemAdd((CFDictionaryRef)add, NULL), errSecSuccess);
    NSMutableDictionary *query = [NSMutableDictionary dictionaryWithDictionary:identity];
    query[S(kSecAttrSynchronizable)] = S(kSecAttrSynchronizableAny);
    query[S(kSecReturnData)] = @YES;
    fetchPassword(cases, @"forms.password_fetch", query, tag);

    /* The access group limits searches and deletes, and is part of the identity. */
    NSMutableDictionary *other = [NSMutableDictionary dictionaryWithDictionary:identity];
    other[S(kSecAttrAccessGroup)] = @"org.darling.keychain-compat.other";
    checkStatus(cases, @"forms.other_group_missing",
                SecItemCopyMatching((CFDictionaryRef)other, NULL), errSecItemNotFound);
    checkStatus(cases, @"forms.other_group_delete_missing",
                SecItemDelete((CFDictionaryRef)other), errSecItemNotFound);
    NSMutableDictionary *otherAdd = [NSMutableDictionary dictionaryWithDictionary:other];
    otherAdd[S(kSecValueData)] = aclTag;
    checkStatus(cases, @"forms.other_group_add", SecItemAdd((CFDictionaryRef)otherAdd, NULL), errSecSuccess);
    checkStatus(cases, @"forms.other_group_delete", SecItemDelete((CFDictionaryRef)other), errSecSuccess);
    fetchPassword(cases, @"forms.password_kept", query, tag);

    /* An item stored without a group is not reached by a query that names one. */
    NSDictionary *plain = @{S(kSecClass): S(kSecClassGenericPassword),
        S(kSecAttrService): @"org.darling.keychain-compat.forms.plain",
        S(kSecAttrAccount): identifier, S(kSecUseDataProtectionKeychain): @YES};
    NSMutableDictionary *plainAdd = [NSMutableDictionary dictionaryWithDictionary:plain];
    plainAdd[S(kSecValueData)] = tag;
    checkStatus(cases, @"forms.ungrouped_add", SecItemAdd((CFDictionaryRef)plainAdd, NULL), errSecSuccess);
    NSMutableDictionary *grouped = [NSMutableDictionary dictionaryWithDictionary:plain];
    grouped[S(kSecAttrAccessGroup)] = @"org.darling.keychain-compat.other";
    checkStatus(cases, @"forms.ungrouped_group_delete_missing",
                SecItemDelete((CFDictionaryRef)grouped), errSecItemNotFound);
    NSMutableDictionary *plainFetch = [NSMutableDictionary dictionaryWithDictionary:plain];
    plainFetch[S(kSecReturnData)] = @YES;
    fetchPassword(cases, @"forms.ungrouped_kept", plainFetch, tag);
    checkStatus(cases, @"forms.ungrouped_delete", SecItemDelete((CFDictionaryRef)plain), errSecSuccess);

    /* macOS does not return the data of several passwords in one call. */
    NSMutableDictionary *every = [NSMutableDictionary dictionaryWithDictionary:query];
    every[S(kSecMatchLimit)] = S(kSecMatchLimitAll);
    checkStatus(cases, @"forms.match_all_data",
                SecItemCopyMatching((CFDictionaryRef)every, NULL), errSecParam);

    /* Outside the store: the stock status must come back, not a parameter error. */
    query[S(kSecReturnPersistentRef)] = @YES;
    OSStatus status = SecItemCopyMatching((CFDictionaryRef)query, NULL);
    record(cases, @"forms.persistent_ref_declined",
           status != errSecSuccess && status != errSecParam, status);
    NSDictionary *rsa = @{S(kSecClass): S(kSecClassKey), S(kSecAttrKeyType): S(kSecAttrKeyTypeRSA),
                          S(kSecUseDataProtectionKeychain): @YES};
    status = SecItemCopyMatching((CFDictionaryRef)rsa, NULL);
    record(cases, @"forms.rsa_declined", status != errSecSuccess && status != errSecParam, status);

    /* macOS rejects a match limit on delete. */
    NSMutableDictionary *limited = [NSMutableDictionary dictionaryWithDictionary:identity];
    limited[S(kSecMatchLimit)] = S(kSecMatchLimitOne);
    checkStatus(cases, @"forms.delete_match_limit", SecItemDelete((CFDictionaryRef)limited), errSecParam);
    checkStatus(cases, @"forms.password_delete", SecItemDelete((CFDictionaryRef)identity), errSecSuccess);
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
           !key && code == errSecNotAvailable, code);
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
        if (create) {
            exerciseQueryForms(cases, identifier);
            exerciseSecureEnclave(cases);
        }

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
