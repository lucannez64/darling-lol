/* Bounded, non-networked plist policy parsing shared by mldr and the server. */
#ifndef DC_ENTITLEMENTS_H
#define DC_ENTITLEMENTS_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <libxml/parser.h>
#include <libxml/tree.h>

static inline xmlNode* dc_next_element(xmlNode* node) {
    while (node && (node->type == XML_COMMENT_NODE || node->type == XML_PI_NODE ||
                    (node->type == XML_TEXT_NODE && xmlIsBlankNode(node)))) node = node->next;
    return node;
}
static inline bool dc_xml_name(xmlNode* node, const char* name) {
    return node && !node->ns && xmlStrEqual(node->name, (const xmlChar*)name);
}
static inline bool dc_entitlement_is_true(const uint8_t* data, size_t len, const char* key) {
    if (!data || !key || !len || len > 1024 * 1024) return false;
    while (len && data[len - 1] == 0) len--;
    xmlDoc* doc = xmlReadMemory((const char*)data, (int)len, "entitlements.plist", NULL,
        XML_PARSE_NONET | XML_PARSE_NOERROR | XML_PARSE_NOWARNING);
    if (!doc) return false;
    bool result = false, seen = false;
    /* A normal plist DOCTYPE is fine; custom entities and external DTDs are not
     * policy input. No DTD loading or entity substitution options are enabled. */
    if (doc->extSubset || (doc->intSubset &&
        (doc->intSubset->entities || doc->intSubset->pentities))) goto done;
    {
        xmlNode* plist = xmlDocGetRootElement(doc);
        if (!dc_xml_name(plist, "plist")) goto done;
        xmlNode* dict = dc_next_element(plist->children);
        if (!dc_xml_name(dict, "dict") || dc_next_element(dict->next)) goto done;
        for (xmlNode* entry = dc_next_element(dict->children); entry;) {
            if (!dc_xml_name(entry, "key") || entry->properties) { result = false; goto done; }
            for (xmlNode* part = entry->children; part; part = part->next) {
                if (part->type != XML_TEXT_NODE && part->type != XML_CDATA_SECTION_NODE) {
                    result = false; goto done;
                }
            }
            xmlNode* value = dc_next_element(entry->next);
            if (!(dc_xml_name(value, "true") || dc_xml_name(value, "false") ||
                  dc_xml_name(value, "string") || dc_xml_name(value, "data") ||
                  dc_xml_name(value, "date") || dc_xml_name(value, "integer") ||
                  dc_xml_name(value, "real") || dc_xml_name(value, "array") ||
                  dc_xml_name(value, "dict"))) { result = false; goto done; }
            xmlChar* name = xmlNodeGetContent(entry);
            bool matches = name && xmlStrEqual(name, (const xmlChar*)key);
            xmlFree(name);
            if (matches) {
                if (seen) { result = false; goto done; }
                seen = true;
                result = dc_xml_name(value, "true") && !value->children && !value->properties;
            }
            entry = dc_next_element(value->next);
        }
    }
done:
    xmlFreeDoc(doc);
    return result;
}
#endif
