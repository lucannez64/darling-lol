"""NSURLCache and NSURLSession keep the on-disk stores macOS 15.5 creates.

On the reference Mac (macOS 15.5) an app using the default shared URL cache has
~/Library/Caches/<bundle id>/Cache.db (WAL, auto_vacuum 2, user_version 203, plus
fsCachedData/ for large bodies) and ~/Library/HTTPStorages/<bundle id>/
httpstorages.sqlite (WAL, auto_vacuum 2) with an alt_services table. The SQL below
is the sqlite_master text measured there (schema only). Source-only: reads the
patched files from patches/cfnetwork.patch and never starts Darling.
"""
import re
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'patches/cfnetwork.patch'

MAC_CACHE_SCHEMA = [
    ('table', 'cfurl_cache_response',
     'CREATE TABLE cfurl_cache_response(entry_ID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, '
     '\t version INTEGER, hash_value INTEGER, storage_policy INTEGER, request_key TEXT UNIQUE, '
     '\t time_stamp NOT NULL DEFAULT CURRENT_TIMESTAMP, partition TEXT)'),
    ('index', 'sqlite_autoindex_cfurl_cache_response_1', None),
    ('index', 'sqlite_autoindex_cfurl_cache_response_2', None),
    ('table', 'sqlite_sequence', 'CREATE TABLE sqlite_sequence(name,seq)'),
    ('table', 'cfurl_cache_blob_data',
     'CREATE TABLE cfurl_cache_blob_data(entry_ID INTEGER PRIMARY KEY, response_object BLOB, '
     'request_object BLOB, \t\t\t  proto_props BLOB, user_info BLOB)'),
    ('table', 'cfurl_cache_receiver_data',
     'CREATE TABLE cfurl_cache_receiver_data(entry_ID INTEGER PRIMARY KEY, isDataOnFS INTEGER, '
     'receiver_data BLOB)'),
    ('index', 'request_key_index', 'CREATE INDEX request_key_index ON cfurl_cache_response(request_key)'),
    ('index', 'time_stamp_index', 'CREATE INDEX time_stamp_index ON cfurl_cache_response(time_stamp)'),
    ('index', 'proto_props_index', 'CREATE INDEX proto_props_index ON cfurl_cache_blob_data(entry_ID)'),
    ('index', 'receiver_data_index',
     'CREATE INDEX receiver_data_index ON cfurl_cache_receiver_data(entry_ID)'),
]

MAC_HTTPSTORAGES_SCHEMA = [
    ('table', 'alt_services',
     'CREATE TABLE alt_services (        partition text NOT NULL,        host text NOT NULL,'
     '        alternateHost text NOT NULL,        port int NOT NULL,        alternatePort int NOT NULL,'
     '        type int NOT NULL,        creation_time int,        expires_time int,'
     '        UNIQUE(partition, host, port, type)        )'),
    ('index', 'sqlite_autoindex_alt_services_1', None),
]


def patched_file(path):
    """Return the added/kept lines of path's hunks in patches/cfnetwork.patch."""
    text = PATCH.read_text()
    start = text.index('+++ b/' + path + '\n')
    end = text.find('\ndiff --git ', start)
    section = text[start:end if end != -1 else len(text)]
    return '\n'.join(line[1:] for line in section.splitlines()[1:]
                     if line[:1] in ('+', ' ') and not line.startswith('+++'))


def c_string(literal_body):
    return literal_body.encode().decode('unicode_escape')


def schema_of(statements):
    db = sqlite3.connect(':memory:')
    for statement in statements:
        db.execute(statement)
    rows = db.execute('SELECT type, name, sql FROM sqlite_master').fetchall()
    db.close()
    return rows


class URLCacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.cache = patched_file('src/URL/CFURLCache.c')

    def cache_statements(self):
        block = re.search(r'CacheSchemaStatements\[\] = \{(.*?)\n\};', self.cache, re.S).group(1)
        return [c_string(body) for body in re.findall(r'^\s*"((?:[^"\\]|\\.)*)",\s*$', block, re.M)]

    def test_cache_db_schema_matches_macos(self):
        statements = self.cache_statements()
        self.assertEqual(len(statements), 7)
        self.assertEqual(schema_of(statements), MAC_CACHE_SCHEMA)

    def test_cache_db_layout_and_pragmas(self):
        self.assertIn('CacheDatabaseName = "Cache.db"', self.cache)
        self.assertIn('CacheFSDataDirectoryName = CFSTR("fsCachedData")', self.cache)
        self.assertIn('CacheDatabaseUserVersion = 203', self.cache)
        self.assertIn('"PRAGMA journal_mode = WAL"', self.cache)
        self.assertIn('"PRAGMA auto_vacuum = 2"', self.cache)
        # The Apportable plist/data file pairs are gone.
        self.assertNotIn('CacheEntryParcelFileExtension', self.cache)
        self.assertNotIn('".plist"', self.cache)

    def test_bodies_are_inline_or_in_fs_cached_data(self):
        self.assertIn('INSERT INTO cfurl_cache_receiver_data(entry_ID, isDataOnFS, receiver_data)', self.cache)
        self.assertIn('CFCachedURLResponseGetDataSize(cachedResponse) > CacheInlineDataLimit', self.cache)
        self.assertIn('"SELECT receiver_data FROM cfurl_cache_receiver_data WHERE entry_ID = ? AND isDataOnFS = 0"',
                      self.cache)
        response = patched_file('src/URL/CFCachedURLResponse.c')
        self.assertIn('_CFCachedURLResponseCreateWithDataLoader', response)
        self.assertIn('if (cachedResponse->dataPath || cachedResponse->loader) {', response)

    def test_only_storage_allowed_responses_reach_disk(self):
        self.assertIn('if (policy == CFURLCacheStorageAllowed && cache->store) {', self.cache)

    def test_httpstorages_schema_matches_macos(self):
        body = re.search(r'HTTPStoragesSchemaStatement =\s*"((?:[^"\\]|\\.)*)";', self.cache).group(1)
        self.assertEqual(schema_of([c_string(body)]), MAC_HTTPSTORAGES_SCHEMA)
        self.assertIn('CFSTR("httpstorages.sqlite")', self.cache)


class DefaultLocationTests(unittest.TestCase):
    def test_shared_cache_defaults_follow_the_macos_sdk(self):
        source = patched_file('src/URL/NSURLCache.m')
        self.assertIn('NSURLCacheDefaultMemoryCapacity = 4 * 1024 * 1024;', source)
        self.assertIn('NSURLCacheDefaultDiskCapacity = 20 * 1024 * 1024;', source)
        self.assertIn('NSString* cachePath = _NSURLStorageDirectory(@"Caches");', source)
        self.assertIn('[[NSBundle mainBundle] bundleIdentifier]', source)
        self.assertIn('[[NSProcessInfo processInfo] processName]', source)
        self.assertNotIn('getenv("CACHEDIR")', source)
        self.assertIn('directoryURL:(NSURL*)directoryURL', source)

    def test_first_session_creates_httpstorages(self):
        source = patched_file('src/URL/NSCFURLSession.m')
        self.assertIn('_NSURLStorageDirectory(@"HTTPStorages")', source)
        self.assertIn('_CFURLStorageEnsureHTTPStorages((CFStringRef)directory);', source)
        self.assertIn('ensureHTTPStorages();', source)

    def test_cfnetwork_links_libsqlite3(self):
        cmake = patched_file('src/CMakeLists.txt')
        self.assertRegex(cmake, r'SystemConfiguration\n\s*sqlite3\n\s*CIRCULAR_DEPENDENCIES')


if __name__ == '__main__':
    unittest.main()
