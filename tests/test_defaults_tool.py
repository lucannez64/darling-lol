"""defaults(1) prints values like macOS 15.5; source-only, never starts Darling.

The formatting core of Foundation's tools/defaults.m is plain C between the
BEGIN/END defaults-format markers. These tests take that section from
patches/foundation.patch, compile it on the host with a small harness and
compare its output with text measured with /usr/bin/defaults on macOS 15.5.
"""
import calendar
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/foundation.patch'
SOURCE = 'tools/defaults.m'
BEGIN = '/* BEGIN defaults-format'
END = '/* END defaults-format */'


def added_file(path):
    """Return the added/kept lines of path's hunks in patches/foundation.patch."""
    text = PATCH.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


def format_section(source):
    start = source.index(BEGIN)
    end = source.index(END, start) + len(END)
    return source[start:end]


def since_2001(year, month, day, hour, minute, second):
    return calendar.timegm((year, month, day, hour, minute, second)) - 978307200


HARNESS_HEAD = r'''
#define _GNU_SOURCE
#include <ctype.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
'''

HARNESS_HELPERS = r'''
static struct dv *node(enum dv_kind kind)
{
  struct dv *v = calloc(1, sizeof *v);
  v->kind = kind;
  return v;
}

static struct dv *S(const char *utf8)
{
  struct dv *v = node(DV_STRING);
  uint16_t *u = calloc(strlen(utf8) + 1, sizeof(uint16_t));
  const unsigned char *p = (const unsigned char *)utf8;
  size_t n = 0;
  while (*p)
    {
      if (*p < 0x80) u[n++] = *p++;
      else if ((*p & 0xe0) == 0xc0) { u[n++] = (uint16_t)((p[0] & 0x1f) << 6 | (p[1] & 0x3f)); p += 2; }
      else { u[n++] = (uint16_t)((p[0] & 0x0f) << 12 | (p[1] & 0x3f) << 6 | (p[2] & 0x3f)); p += 3; }
    }
  v->chars = u;
  v->length = n;
  return v;
}

static struct dv *I(long long i) { struct dv *v = node(DV_INTEGER); v->integer = i; return v; }
static struct dv *R(double d) { struct dv *v = node(DV_REAL); v->real = d; return v; }
static struct dv *F(float f) { struct dv *v = node(DV_FLOAT32); v->real = f; return v; }
static struct dv *B(int b) { struct dv *v = node(DV_BOOL); v->integer = b; return v; }
static struct dv *D(double t) { struct dv *v = node(DV_DATE); v->real = t; return v; }

static struct dv *X(const char *hex)
{
  struct dv *v = node(DV_DATA);
  unsigned char *bytes = malloc(strlen(hex) / 2 + 1);
  v->length = (size_t)parse_hex_bytes(hex, bytes);
  v->bytes = bytes;
  return v;
}

static struct dv *A(int n, ...)
{
  struct dv *v = node(DV_ARRAY);
  va_list ap;
  int i;
  v->items = calloc(n + 1, sizeof(struct dv));
  v->count = n;
  va_start(ap, n);
  for (i = 0; i < n; i++) v->items[i] = *va_arg(ap, struct dv *);
  va_end(ap);
  return v;
}

static struct dv *M(int n, ...)
{
  struct dv *v = node(DV_DICT);
  va_list ap;
  int i;
  v->items = calloc(2 * n + 1, sizeof(struct dv));
  v->count = n;
  va_start(ap, n);
  for (i = 0; i < 2 * n; i++) v->items[i] = *va_arg(ap, struct dv *);
  va_end(ap);
  return v;
}

static void show(struct dv *v)
{
  struct dbuf b = { 0 };
  dv_describe(&b, v);
  fputs(b.p ? b.p : "", stdout);
  fputs("\n<<END>>\n", stdout);
}
'''

LANGUAGES = '(\n    "en-PL",\n    "pl-PL"\n)'

RIOT_CLIENT = ('{\n'
               '    AppleTextDirection = 1;\n'
               '    NSForceRightToLeftWritingDirection = 0;\n'
               '    NSTreatUnknownArgumentsAsOpen = NO;\n'
               '}')

# Excerpts of `defaults read -g` and other domains on the reference Mac.
GLOBAL = (
    '{\n'
    '    AppleInterfaceStyle = Dark;\n'
    '    AppleLanguages =     (\n'
    '        "en-PL",\n'
    '        "pl-PL"\n'
    '    );\n'
    '    AppleLanguagesDidMigrate = "12.0.1";\n'
    '    AppleLanguagesSchemaVersion = 5400;\n'
    '    AppleLocale = "en_PL";\n'
    '    BlockedApps =     (\n'
    '    );\n'
    '    "KB_DoubleQuoteOption" = "\\\\U201cabc\\\\U201d";\n'
    '    "KB_SpellingLanguage" =     {\n'
    '        "KB_SpellingLanguageIsAutomatic" = 1;\n'
    '    };\n'
    '    NSLinguisticDataAssetsRequestLastInterval = 86400;\n'
    '    NSLinguisticDataAssetsRequestTime = "2026-11-25 23:59:02 +0000";\n'
    '    NSPreferredWebServices =     {\n'
    '        NSWebServicesProviderWebSearch =         {\n'
    '            NSDefaultDisplayName = Google;\n'
    '            NSProviderIdentifier = "com.google.www";\n'
    '        };\n'
    '    };\n'
    '    NSUserDictionaryReplacementItems =     (\n'
    '                {\n'
    '            on = 1;\n'
    '            replace = omw;\n'
    '            with = "On my way!";\n'
    '        }\n'
    '    );\n'
    '    NavPanelFileListModeForOpenMode = 2;\n'
    '    "_AKBAACertMarkerKey" = {length = 32, bytes = 0xc06baa63 e7104ede 15ed1c2b 6de2eed9'
    ' ... 4c3070bf 107171c7 };\n'
    '    "com.apple.finder.SyncExtensions" =     {\n'
    '        collaborationMap =         {\n'
    '        };\n'
    '        dirMap =         {\n'
    '        };\n'
    '    };\n'
    '    "com.apple.mouse.scaling" = "0.6875";\n'
    '    "com.apple.sound.beep.volume" = "0.9792427";\n'
    '    "home-sharing-group-id" = "";\n'
    '    shouldShowRSVPDataDetectors = 0;\n'
    '}')

ESCAPES = (
    '{\n'
    '    CRLRemoteDefaultsETag = "\\\\"145b-616511aee54d6\\\\"";\n'
    '    "User Settings.Browser\\\\U00b7lastOpenedDir" = "/Users/pedro/Downloads";\n'
    '    "amplitude-$default_instance" = 2;\n'
    '    "free.intruder.matchgrep.item12" = "c:\\\\\\\\";\n'
    '    idtoken = "ab\\\\ncd";\n'
    '    offset = "-14.89999389648438";\n'
    '    ratio = "1.6";\n'
    '    third = "0.3333333432674408";\n'
    '}')

CASES = [
    # (C expression, expected output)
    ('A(2, S("en-PL"), S("pl-PL"))', LANGUAGES),
    ('M(3, S("NSTreatUnknownArgumentsAsOpen"), S("NO"), S("AppleTextDirection"), B(1),'
     ' S("NSForceRightToLeftWritingDirection"), B(0))', RIOT_CLIENT),
    ('M(19,'
     ' S("shouldShowRSVPDataDetectors"), B(0),'
     ' S("com.apple.sound.beep.volume"), F(0.97924268245697021f),'
     ' S("com.apple.mouse.scaling"), R(0.6875),'
     ' S("home-sharing-group-id"), S(""),'
     ' S("com.apple.finder.SyncExtensions"), M(2, S("dirMap"), M(0), S("collaborationMap"), M(0)),'
     ' S("_AKBAACertMarkerKey"), X("c06baa63e7104ede15ed1c2b6de2eed900112233445566774c3070bf107171c7"),'
     ' S("NavPanelFileListModeForOpenMode"), I(2),'
     ' S("NSUserDictionaryReplacementItems"), A(1, M(3, S("with"), S("On my way!"), S("on"), I(1),'
     '   S("replace"), S("omw"))),'
     ' S("NSPreferredWebServices"), M(1, S("NSWebServicesProviderWebSearch"), M(2,'
     '   S("NSProviderIdentifier"), S("com.google.www"), S("NSDefaultDisplayName"), S("Google"))),'
     ' S("NSLinguisticDataAssetsRequestTime"), D(%d),'
     ' S("NSLinguisticDataAssetsRequestLastInterval"), R(86400),'
     ' S("KB_SpellingLanguage"), M(1, S("KB_SpellingLanguageIsAutomatic"), B(1)),'
     ' S("KB_DoubleQuoteOption"), S("\\u201cabc\\u201d"),'
     ' S("BlockedApps"), A(0),'
     ' S("AppleLocale"), S("en_PL"),'
     ' S("AppleLanguagesDidMigrate"), S("12.0.1"),'
     ' S("AppleLanguages"), A(2, S("en-PL"), S("pl-PL")),'
     ' S("AppleLanguagesSchemaVersion"), I(5400),'
     ' S("AppleInterfaceStyle"), S("Dark"))' % since_2001(2026, 11, 25, 23, 59, 2), GLOBAL),
    ('M(8, S("CRLRemoteDefaultsETag"), S("\\"145b-616511aee54d6\\""),'
     ' S("free.intruder.matchgrep.item12"), S("c:\\\\"),'
     ' S("idtoken"), S("ab\\ncd"),'
     ' S("amplitude-$default_instance"), I(2),'
     ' S("User Settings.Browser\\u00b7lastOpenedDir"), S("/Users/pedro/Downloads"),'
     ' S("offset"), R(-14.899993896484375),'
     ' S("ratio"), F(1.6000000238418579f),'
     ' S("third"), R(0.3333333432674408))', ESCAPES),
    # `defaults read -g AppleLocale` and other single values print bare.
    ('S("en_PL")', 'en_PL'),
    ('S("\\"145b-616511aee54d6\\"")', '"145b-616511aee54d6"'),
    ('S("Piotr\'s Kindle")', "Piotr's Kindle"),
    ('S("\\u201cabc\\u201d")', '\\u201cabc\\u201d'),
    ('R(0.6875)', '0.6875'),
    ('D(%d)' % since_2001(2026, 11, 25, 23, 59, 2), '2026-11-25 23:59:02 +0000'),
    ('X("c06baa63e7104ede15ed1c2b6de2eed900112233445566774c3070bf107171c7")',
     '{length = 32, bytes = 0xc06baa63 e7104ede 15ed1c2b 6de2eed9 ... 4c3070bf 107171c7 }'),
    ('X("")', '{length = 0, bytes = 0x}'),
    ('X("7b7d")', '{length = 2, bytes = 0x7b7d}'),
    ('X("5b5b3237352c3235355d2c5b3839302c3535325d5d")',
     '{length = 21, bytes = 0x5b5b3237352c3235355d2c5b3839302c3535325d5d}'),
    ('X("4056617269616e74280000004301c3bf0000000000000000000029")',
     '{length = 27, bytes = 0x40566172 69616e74 28000000 4301c3bf ... 00000000 00000029 }'),
]


def compiler():
    for name in ('cc', 'clang', 'gcc'):
        path = shutil.which(name)
        if path:
            return path
    return None


class DefaultsSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = added_file(SOURCE)

    def test_reads_and_writes_through_cfpreferences(self):
        for call in ('CFPreferencesCopyValue(', 'CFPreferencesCopyMultiple(NULL',
                     'CFPreferencesSetValue(', 'CFPreferencesSetMultiple(',
                     'CFPreferencesSynchronize(', 'CFPreferencesCopyApplicationList('):
            self.assertIn(call, self.source)
        self.assertNotIn('NSUserDefaults', self.source)

    def test_domain_names_match_macos(self):
        for name in ('"-g"', '"-globalDomain"', '"NSGlobalDomain"', '"Apple Global Domain"',
                     '"-app"', '"-currentHost"', '"-host"', '"kCFPreferencesAnyApplication"'):
            self.assertIn(name, self.source)
        # A path domain works with or without its .plist extension.
        self.assertIn("arg[0] == '/'", self.source)
        self.assertIn('".plist"', self.source)

    def test_commands_and_flags(self):
        for word in ('"read"', '"read-type"', '"write"', '"delete"', '"delete-all"', '"rename"',
                     '"import"', '"export"', '"domains"', '"find"', '"help"',
                     '"-string"', '"-data"', '"-int"', '"-integer"', '"-float"', '"-bool"',
                     '"-boolean"', '"-date"', '"-array"', '"-array-add"', '"-dict"', '"-dict-add"'):
            self.assertIn(word, self.source)

    def test_messages_and_exit_codes_match_macos(self):
        # Measured on macOS 15.5; NSLog-style prefix on stderr, exit 1.
        self.assertIn('"\\nThe domain/default pair of (%s, %s) does not exist"', self.source)
        self.assertIn('"\\nDomain %s does not exist"', self.source)
        self.assertIn('"\\nDomain (%s) not found.\\nDefaults have not been changed."', self.source)
        self.assertIn('"Key %s does not exist in domain %s; leaving defaults unchanged"', self.source)
        self.assertIn('"Couldn\'t find an application named \\"%s\\"; defaults unchanged"', self.source)
        self.assertIn('"No domain, key, nor value containing \'%s\'"', self.source)
        self.assertIn('"Found %ld keys in domain \'%s\': "', self.source)
        self.assertIn('"Type is %s\\n"', self.source)
        self.assertIn('"%s.%03d defaults[%d:%llu] "', self.source)
        self.assertIn('fprintf(stderr', self.source)
        # Usage goes to stdout: exit 255, or 0 for `defaults help`.
        self.assertIn('"Command line interface to a user\'s defaults.\\n"', self.source)
        self.assertIn('return 255;', self.source)


class DefaultsFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = compiler()
        if cc is None:
            raise unittest.SkipTest('no C compiler')
        section = format_section(added_file(SOURCE))
        body = ['int main(void)', '{']
        body += ['  show(%s);' % expr for expr, _ in CASES]
        types = ['S("x")', 'I(1)', 'R(1.5)', 'F(1.5f)', 'B(1)', 'D(0)', 'X("00")', 'A(0)', 'M(0)']
        body += ['  puts(dv_type_name(%s));' % t for t in types]
        body += ['  printf("%d %d %d %d %d %d\\n", parse_bool_word("YES"), parse_bool_word("true"),'
                 ' parse_bool_word("No"), parse_bool_word("0"), parse_bool_word("maybe"),'
                 ' parse_bool_word("1"));',
                 '  printf("%ld %ld %ld\\n", parse_hex_bytes("<dead beef>", NULL),'
                 ' parse_hex_bytes("abc", NULL), parse_hex_bytes("zz", NULL));',
                 '  return 0;', '}']
        program = HARNESS_HEAD + section + HARNESS_HELPERS + '\n'.join(body) + '\n'
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'harness.c')
            exe = os.path.join(tmp, 'harness')
            Path(src).write_text(program)
            subprocess.run([cc, '-std=c99', '-Wall', '-Werror', '-Wno-unused-function',
                            '-o', exe, src], check=True)
            cls.output = subprocess.run([exe], check=True, capture_output=True,
                                        text=True).stdout

    def test_values_print_like_macos(self):
        printed = self.output.split('\n<<END>>\n')
        for (expr, expected), got in zip(CASES, printed):
            with self.subTest(expr=expr[:60]):
                self.assertEqual(got, expected)

    def test_type_names_and_argument_parsing(self):
        tail = self.output.split('\n<<END>>\n')[-1].splitlines()
        self.assertEqual(tail[:9], ['string', 'integer', 'float', 'float', 'boolean', 'date',
                                    'data', 'array', 'dictionary'])
        self.assertEqual(tail[9], '1 1 0 0 -1 1')
        self.assertEqual(tail[10], '4 -1 -1')


if __name__ == '__main__':
    unittest.main()
