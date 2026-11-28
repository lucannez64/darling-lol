"""Compatibility-language check used by validate.py (docs/COMPLIANCE.md).

Framing words always fail. Words that also name ordinary engineering
("compositor bypass") fail only next to anti-cheat, detection, ban or
telemetry context. A sentence that states a negation ("not intended to ...")
is a non-goal and passes; the negation must be in the same sentence as the
word, which may start on the previous line.
"""
import re

FRAMING = re.compile(r'\b(?:stealth\w*|evasion|evad\w*|unbannable|undetectable)\b', re.I)
LOADED = re.compile(r'\b(?:spoof\w*|bypass\w*|circumvent\w*)\b', re.I)
RISK = re.compile(r'anti-?cheat|vanguard|detect|\bban(?:s|ned)?\b|telemetry|attestation|\bdrm\b', re.I)
NEGATION = re.compile(r"\b(?:not|never|avoid|without|out of scope|instead of)\b|n't\b", re.I)
SENTENCE_END = re.compile(r'[.!?](?:\s|$)')

EXEMPT = {
    'AGENTS.md': 'lists the words to avoid',
    'CONTRIBUTING.md': 'lists the words to avoid',
    'docs/COMPLIANCE.md': 'defines the wording policy',
    'docs/skills/open-pr/SKILL.md': 'restates the wording policy',
    'docs/security/ATTESTATION_143_FINDINGS.md': 'quotes the client config key and log line verbatim',
    'scripts/compat_language.py': 'defines this check',
    'tests/test_compat_language.py': 'tests this check',
}


def _sentence(text, start, end):
    """The sentence of `text` that contains text[start:end]."""
    before = [m.end() for m in SENTENCE_END.finditer(text, 0, start)]
    after = SENTENCE_END.search(text, end)
    return text[before[-1] if before else 0:after.end() if after else len(text)]


def errors(name, text):
    if name in EXEMPT:
        return []
    out = []
    prev = ''
    for number, line in enumerate(text.splitlines(), 1):
        context = prev + ' ' + line
        offset = len(prev) + 1
        hits = list(FRAMING.finditer(line))
        if RISK.search(line):
            hits += LOADED.finditer(line)
        for hit in hits:
            sentence = _sentence(context, offset + hit.start(), offset + hit.end())
            if not NEGATION.search(sentence):
                out.append(f'{name}:{number}: use compatibility language '
                           f'(docs/COMPLIANCE.md): {line.strip()[:100]}')
                break
        prev = line
    return out
