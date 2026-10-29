#include "dc_entitlements.h"
#include <assert.h>
#include <stdio.h>
#define KEY "com.apple.security.cs.disable-library-validation"
static void check(const char* xml, bool expected) {
    bool got = dc_entitlement_is_true((const uint8_t*)xml, strlen(xml), KEY);
    if (got != expected) { fprintf(stderr, "unexpected policy for %s\n", xml); assert(got == expected); }
}
int main(void) {
    check("<plist><dict><key>" KEY "</key><true/></dict></plist>", true);
    check("<?xml version=\"1.0\"?><!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\"><plist version=\"1.0\"><dict><key>" KEY "</key><true/></dict></plist>", true);
    check("<plist><dict><!-- <key>" KEY "</key><true/> --><key>" KEY "</key><false/></dict></plist>", false);
    check("<plist><dict><key>nested</key><dict><key>" KEY "</key><true/></dict></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><true/><key>" KEY "</key><false/></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><false/><key>" KEY "</key><true/></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><string>true</string></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><integer>1</integer></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><true/><key>missing-value</key></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><true/>", false);
    check("<!DOCTYPE plist [<!ENTITY custom '" KEY "'>]><plist><dict><key>&custom;</key><true/></dict></plist>", false);
    check("<!DOCTYPE plist [<!ENTITY external SYSTEM 'file:///etc/passwd'>]><plist><dict><key>" KEY "</key><true/></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><true/></dict><dict/></plist>", false);
    check("<plist><array><key>" KEY "</key><true/></array></plist>", false);
    check("<plist><dict><key><string>" KEY "</string></key><true/></dict></plist>", false);
    check("<plist><dict>junk<key>" KEY "</key><true/></dict></plist>", false);
    check("<plist><dict><key>" KEY "</key><true/>junk</dict></plist>", false);
    check("<plist><dict><key>unrelated</key><unknown/><key>" KEY "</key><true/></dict></plist>", false);
    assert(!dc_entitlement_is_true(NULL, 0, KEY));
    assert(!dc_entitlement_is_true((const uint8_t*)"<plist/>", 1024 * 1024 + 1, KEY));
    puts("PASS top-level entitlement policy, comments, duplicates, malformed input and entities");
}
