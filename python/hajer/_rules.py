"""GENERATED from the Hajer platform's redaction catalog (redaction-rules-v6.json). Never hand-edited.

The client removes the same shapes the server would, under the same catalog id, so a stored observation's
`clientRedaction` report names a rule set that was really in force. Source: the platform's detector catalog
plus the credential and contact rules it declares alongside it. The generator runs in the platform
repository (`contract/README.md`).

Every pattern is compiled **here**, at import, exactly once per process: compiling inside the walk would
recompile every rule for every string value of every submission, inside somebody else's request path.
Both generation-time lints (nested quantifiers, timing fuzz) ran before this file was written.
"""

import re
from dataclasses import dataclass
from typing import Final

#: The catalog this rule set came from, as `<id>@<version>`. Reported on the wire and by `hajer doctor`.
CATALOG_ID: Final[str] = 'redaction-rules@6'

#: A digest over the catalog, the backend module and the generator. `--check` compares this when the
#: interpreter it runs on cannot parse the backend's own syntax.
SOURCE_DIGEST: Final[str] = "sha256:e4cd54528ebc67ba4ebd264e16f5085a03fb933f227ed2f5dbe7918ac3aac32a"

#: How many digits make a run account-like, substituted into the catalog's own pattern.
MIN_ACCOUNT_DIGITS: Final[int] = 12


@dataclass(frozen=True, slots=True)
class Rule:
    """One shape, what admits it, what replaces it, and what a receipt calls it."""

    rule_id: str
    category: str
    pattern: re.Pattern[str]
    validator: str
    placeholder: str
    phase: str
    default_on: bool


#: In the order they run, which is the order the backend runs them in: credentials first (a bearer token
#: made of digits is not an account number), then the catalog's specific shapes, then the two contact
#: classes, then the loosest rule of all.
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        rule_id='anthropic-key',
        category='ANTHROPIC_KEY',
        pattern=re.compile('sk-ant-[A-Za-z0-9_\\-]{8,}'),
        validator='NONE',
        placeholder='[redacted:ANTHROPIC_KEY]',
        phase='CREDENTIAL',
        default_on=True,
    ),
    Rule(
        rule_id='openai-key',
        category='OPENAI_KEY',
        pattern=re.compile('sk-[A-Za-z0-9_\\-]{8,}'),
        validator='NONE',
        placeholder='[redacted:OPENAI_KEY]',
        phase='CREDENTIAL',
        default_on=True,
    ),
    Rule(
        rule_id='hajer-key',
        category='HAJER_KEY',
        pattern=re.compile('hjk_[A-Za-z0-9_\\-]{8,}'),
        validator='NONE',
        placeholder='[redacted:HAJER_KEY]',
        phase='CREDENTIAL',
        default_on=True,
    ),
    Rule(
        rule_id='aws-access-key-id',
        category='AWS_ACCESS_KEY_ID',
        pattern=re.compile('\\b(?:AKIA|ASIA)[0-9A-Z]{16}\\b'),
        validator='NONE',
        placeholder='[redacted:AWS_ACCESS_KEY_ID]',
        phase='CREDENTIAL',
        default_on=True,
    ),
    Rule(
        rule_id='bearer-token',
        category='BEARER_TOKEN',
        pattern=re.compile('(?i)\\b(bearer\\s+)[A-Za-z0-9._~+/=\\-]{8,}'),
        validator='NONE',
        placeholder='[redacted:BEARER_TOKEN]',
        phase='CREDENTIAL',
        default_on=True,
    ),
    Rule(
        rule_id='iban-mod97',
        category='IBAN',
        pattern=re.compile('\\b[A-Z]{2}\\d{2}(?:[ ]?[A-Z0-9]){11,30}\\b'),
        validator='IBAN_MOD97',
        placeholder='[redacted:IBAN]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='vin-check-digit',
        category='VIN',
        pattern=re.compile('\\b(?=[0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\\b'),
        validator='VIN_CHECK',
        placeholder='[redacted:VIN]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='card-luhn-with-trailing-digits',
        category='CARD',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?:\\d(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)?){12,18}\\d(?:(?:(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)|/)\\d{1,4}){1,4}(?![ \\t\\xa0._/-]*\\d)'),
        validator='LUHN_PREFIX',
        placeholder='[redacted:CARD]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='card-luhn',
        category='CARD',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])(?:\\d(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)?){12,18}\\d(?![\\d])(?![ .\\-][\\d])'),
        validator='LUHN',
        placeholder='[redacted:CARD]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='card-luhn-single-separator',
        category='CARD',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])(?:\\d[ \\-]?){12,18}\\d(?![\\d])(?![ .\\-][\\d])'),
        validator='LUHN',
        placeholder='[redacted:CARD]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='card-luhn-with-trailing-digits-single-separator',
        category='CARD',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?:\\d[ \\-]?){12,18}\\d(?:[ \\-/]\\d{1,4}){1,4}(?!\\d)(?![ \\-/]\\d)'),
        validator='LUHN_PREFIX',
        placeholder='[redacted:CARD]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='us-ssn',
        category='US_SSN',
        pattern=re.compile('\\b(?!000|666|9\\d\\d)\\d{3}-(?!00)\\d{2}-(?!0000)\\d{4}\\b'),
        validator='NONE',
        placeholder='[redacted:US_SSN]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='national-id-ca-sin',
        category='NATIONAL_ID',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])\\d{3}[ -]\\d{3}[ -]\\d{3}(?![\\d])(?![ .\\-][\\d])'),
        validator='LUHN',
        placeholder='[redacted:NATIONAL_ID]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='national-id-br-cpf',
        category='NATIONAL_ID',
        pattern=re.compile('\\b\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}\\b'),
        validator='NONE',
        placeholder='[redacted:NATIONAL_ID]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='national-id-in-aadhaar',
        category='NATIONAL_ID',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])[2-9]\\d{3}[ -]\\d{4}[ -]\\d{4}(?![\\d])(?![ .\\-][\\d])'),
        validator='NONE',
        placeholder='[redacted:NATIONAL_ID]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='national-id-fr-nir',
        category='NATIONAL_ID',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])[12][ .]?\\d{2}[ .]?(?:0[1-9]|1[0-2])[ .]?(?:\\d{2}|2[AB])[ .]?\\d{3}[ .]?\\d{3}[ .]?\\d{2}(?![\\d])(?![ .\\-][\\d])'),
        validator='NONE',
        placeholder='[redacted:NATIONAL_ID]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='national-id-uk-nino',
        category='NATIONAL_ID',
        pattern=re.compile('\\b[A-CEGHJ-PR-TW-Z]{2}[ ]?\\d{2}[ ]?\\d{2}[ ]?\\d{2}[ ]?[A-D]\\b'),
        validator='NONE',
        placeholder='[redacted:NATIONAL_ID]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='postal-address-with-postcode',
        category='POSTAL_ADDRESS',
        pattern=re.compile("\\b\\d{1,5}[A-Z]?[ ](?:[A-Z][A-Za-z'\\-]{1,15}[ ]){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Parkway|Pkwy)\\b[.,]{0,2}(?:[ ][A-Za-z'\\-]{1,20},?){0,3}[ ](?:\\d{5}(?:-\\d{4})?|[A-Z]{1,2}\\d[A-Z\\d]?[ ]\\d[A-Z]{2})\\b"),
        validator='NONE',
        placeholder='[redacted:POSTAL_ADDRESS]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='date-of-birth-phrase',
        category='DATE_OF_BIRTH',
        pattern=re.compile('(?i)\\b(?:date of birth|birth ?date|born on|dob)\\b[ :=]{0,3}(?:\\d{1,4}[/.\\-]\\d{1,2}[/.\\-]\\d{2,4}|(?:\\d{1,2} )?(?:Jan|Feb|Mar|Apr|May|Ju[nl]|Aug|Sep|Oct|Nov|Dec)[a-z]{0,6}\\.?,?(?: \\d{1,2})?,? \\d{4})'),
        validator='NONE',
        placeholder='[redacted:DATE_OF_BIRTH]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='person-name-honorific',
        category='PERSON_NAME',
        pattern=re.compile('\\b(?:(?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Hon)\\.|(?:Miss|Sir|Dame|Lord|Lady|Madam|Professor|Doctor))[ ](?:[A-Z]\\.[ ])?[A-Z][a-z]{1,19}(?:[ ](?:[A-Z]\\.|[A-Z][a-z]{1,19})){0,2}'),
        validator='NONE',
        placeholder='[redacted:PERSON_NAME]',
        phase='PRE_PII',
        default_on=True,
    ),
    Rule(
        rule_id='email',
        category='EMAIL',
        pattern=re.compile('[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}'),
        validator='NONE',
        placeholder='[redacted:EMAIL]',
        phase='CONTACT',
        default_on=True,
    ),
    Rule(
        rule_id='phone',
        category='PHONE',
        pattern=re.compile('(?<![\\d\\-])(?:\\+\\d{1,3}[ .\\-]?)?(?:\\(\\d{3}\\)[ .\\-]?|\\d{3}[ .\\-])\\d{3}[ .\\-]?\\d{4}(?![\\d\\-])'),
        validator='NONE',
        placeholder='[redacted:PHONE]',
        phase='CONTACT',
        default_on=True,
    ),
    Rule(
        rule_id='account-like-digits',
        category='ACCOUNT_LIKE',
        pattern=re.compile('(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?=(?:\\d[ \\-]?){12,})\\d{4,}(?:[ \\-]\\d{4,})+(?![\\d\\-])'),
        validator='NONE',
        placeholder='[redacted:ACCOUNT_LIKE]',
        phase='POST_PII',
        default_on=True,
    ),
)

#: Every category any rule can report, for a policy that wants to switch one off by name.
CATEGORIES: Final[frozenset[str]] = frozenset(rule.category for rule in RULES)

#: Every placeholder this rule set writes. `test_idempotent_and_placeholders_match_nothing` asserts that
#: no rule matches any of them, which is what makes a second pass over a redacted document a no-op.
PLACEHOLDERS: Final[frozenset[str]] = frozenset(rule.placeholder for rule in RULES)
