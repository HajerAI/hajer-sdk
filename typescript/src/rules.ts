// GENERATED from the Hajer platform's redaction catalog (redaction-rules-v6.json). Never hand-edited.
// Source: the platform's detector catalog plus the credential and contact rules it declares alongside
// it — the same inputs `python/hajer/_rules.py` is generated from, so the two clients cannot disagree
// about what a `clientRedaction` report names. The generator runs in the platform repository.
//
// Every pattern is constructed once at module load. Building a RegExp inside the walk would rebuild
// every rule for every string value of every submission, inside somebody else's request path. Both
// generation-time lints (nested quantifiers, timing fuzz) ran over these patterns before this file
// was written; a Python inline `(?i)` becomes the literal's own `i` flag and nothing else moves.

/** One shape, what admits it, what replaces it, and what a receipt calls it. */
export interface Rule {
  readonly ruleId: string;
  readonly category: string;
  readonly pattern: RegExp;
  readonly validator: string;
  readonly placeholder: string;
  readonly phase: string;
  readonly defaultOn: boolean;
}

/** The catalog this rule set came from, as `<id>@<version>`. Reported on the wire. */
export const CATALOG_ID = "redaction-rules@6";

/** A digest over the catalog, the backend module and the generator. `--check` compares it. */
export const SOURCE_DIGEST = "sha256:e4cd54528ebc67ba4ebd264e16f5085a03fb933f227ed2f5dbe7918ac3aac32a";

/** How many digits make a run account-like, substituted into the catalog's own pattern. */
export const MIN_ACCOUNT_DIGITS = 12;

/**
 * In the order they run, which is the order the backend runs them in: credentials first, then the
 * catalog's specific shapes, then the two contact classes, then the loosest rule of all.
 */
export const RULES: readonly Rule[] = [
  {
    ruleId: "anthropic-key",
    category: "ANTHROPIC_KEY",
    pattern: new RegExp("sk-ant-[A-Za-z0-9_\\-]{8,}", "g"),
    validator: "NONE",
    placeholder: "[redacted:ANTHROPIC_KEY]",
    phase: "CREDENTIAL",
    defaultOn: true,
  },
  {
    ruleId: "openai-key",
    category: "OPENAI_KEY",
    pattern: new RegExp("sk-[A-Za-z0-9_\\-]{8,}", "g"),
    validator: "NONE",
    placeholder: "[redacted:OPENAI_KEY]",
    phase: "CREDENTIAL",
    defaultOn: true,
  },
  {
    ruleId: "hajer-key",
    category: "HAJER_KEY",
    pattern: new RegExp("hjk_[A-Za-z0-9_\\-]{8,}", "g"),
    validator: "NONE",
    placeholder: "[redacted:HAJER_KEY]",
    phase: "CREDENTIAL",
    defaultOn: true,
  },
  {
    ruleId: "aws-access-key-id",
    category: "AWS_ACCESS_KEY_ID",
    pattern: new RegExp("\\b(?:AKIA|ASIA)[0-9A-Z]{16}\\b", "g"),
    validator: "NONE",
    placeholder: "[redacted:AWS_ACCESS_KEY_ID]",
    phase: "CREDENTIAL",
    defaultOn: true,
  },
  {
    ruleId: "bearer-token",
    category: "BEARER_TOKEN",
    pattern: new RegExp("\\b(bearer\\s+)[A-Za-z0-9._~+/=\\-]{8,}", "gi"),
    validator: "NONE",
    placeholder: "[redacted:BEARER_TOKEN]",
    phase: "CREDENTIAL",
    defaultOn: true,
  },
  {
    ruleId: "iban-mod97",
    category: "IBAN",
    pattern: new RegExp("\\b[A-Z]{2}\\d{2}(?:[ ]?[A-Z0-9]){11,30}\\b", "g"),
    validator: "IBAN_MOD97",
    placeholder: "[redacted:IBAN]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "vin-check-digit",
    category: "VIN",
    pattern: new RegExp("\\b(?=[0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\\b", "g"),
    validator: "VIN_CHECK",
    placeholder: "[redacted:VIN]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "card-luhn-with-trailing-digits",
    category: "CARD",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?:\\d(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)?){12,18}\\d(?:(?:(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)|/)\\d{1,4}){1,4}(?![ \\t\\xa0._/-]*\\d)", "g"),
    validator: "LUHN_PREFIX",
    placeholder: "[redacted:CARD]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "card-luhn",
    category: "CARD",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])(?:\\d(?:[ \\t\\xa0]{1,2}|[ \\t\\xa0]?[._-][ \\t\\xa0]?)?){12,18}\\d(?![\\d])(?![ .\\-][\\d])", "g"),
    validator: "LUHN",
    placeholder: "[redacted:CARD]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "card-luhn-single-separator",
    category: "CARD",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])(?:\\d[ \\-]?){12,18}\\d(?![\\d])(?![ .\\-][\\d])", "g"),
    validator: "LUHN",
    placeholder: "[redacted:CARD]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "card-luhn-with-trailing-digits-single-separator",
    category: "CARD",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?:\\d[ \\-]?){12,18}\\d(?:[ \\-/]\\d{1,4}){1,4}(?!\\d)(?![ \\-/]\\d)", "g"),
    validator: "LUHN_PREFIX",
    placeholder: "[redacted:CARD]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "us-ssn",
    category: "US_SSN",
    pattern: new RegExp("\\b(?!000|666|9\\d\\d)\\d{3}-(?!00)\\d{2}-(?!0000)\\d{4}\\b", "g"),
    validator: "NONE",
    placeholder: "[redacted:US_SSN]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "national-id-ca-sin",
    category: "NATIONAL_ID",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])\\d{3}[ -]\\d{3}[ -]\\d{3}(?![\\d])(?![ .\\-][\\d])", "g"),
    validator: "LUHN",
    placeholder: "[redacted:NATIONAL_ID]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "national-id-br-cpf",
    category: "NATIONAL_ID",
    pattern: new RegExp("\\b\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}\\b", "g"),
    validator: "NONE",
    placeholder: "[redacted:NATIONAL_ID]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "national-id-in-aadhaar",
    category: "NATIONAL_ID",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])[2-9]\\d{3}[ -]\\d{4}[ -]\\d{4}(?![\\d])(?![ .\\-][\\d])", "g"),
    validator: "NONE",
    placeholder: "[redacted:NATIONAL_ID]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "national-id-fr-nir",
    category: "NATIONAL_ID",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])[12][ .]?\\d{2}[ .]?(?:0[1-9]|1[0-2])[ .]?(?:\\d{2}|2[AB])[ .]?\\d{3}[ .]?\\d{3}[ .]?\\d{2}(?![\\d])(?![ .\\-][\\d])", "g"),
    validator: "NONE",
    placeholder: "[redacted:NATIONAL_ID]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "national-id-uk-nino",
    category: "NATIONAL_ID",
    pattern: new RegExp("\\b[A-CEGHJ-PR-TW-Z]{2}[ ]?\\d{2}[ ]?\\d{2}[ ]?\\d{2}[ ]?[A-D]\\b", "g"),
    validator: "NONE",
    placeholder: "[redacted:NATIONAL_ID]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "postal-address-with-postcode",
    category: "POSTAL_ADDRESS",
    pattern: new RegExp("\\b\\d{1,5}[A-Z]?[ ](?:[A-Z][A-Za-z'\\-]{1,15}[ ]){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Parkway|Pkwy)\\b[.,]{0,2}(?:[ ][A-Za-z'\\-]{1,20},?){0,3}[ ](?:\\d{5}(?:-\\d{4})?|[A-Z]{1,2}\\d[A-Z\\d]?[ ]\\d[A-Z]{2})\\b", "g"),
    validator: "NONE",
    placeholder: "[redacted:POSTAL_ADDRESS]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "date-of-birth-phrase",
    category: "DATE_OF_BIRTH",
    pattern: new RegExp("\\b(?:date of birth|birth ?date|born on|dob)\\b[ :=]{0,3}(?:\\d{1,4}[/.\\-]\\d{1,2}[/.\\-]\\d{2,4}|(?:\\d{1,2} )?(?:Jan|Feb|Mar|Apr|May|Ju[nl]|Aug|Sep|Oct|Nov|Dec)[a-z]{0,6}\\.?,?(?: \\d{1,2})?,? \\d{4})", "gi"),
    validator: "NONE",
    placeholder: "[redacted:DATE_OF_BIRTH]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "person-name-honorific",
    category: "PERSON_NAME",
    pattern: new RegExp("\\b(?:(?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Hon)\\.|(?:Miss|Sir|Dame|Lord|Lady|Madam|Professor|Doctor))[ ](?:[A-Z]\\.[ ])?[A-Z][a-z]{1,19}(?:[ ](?:[A-Z]\\.|[A-Z][a-z]{1,19})){0,2}", "g"),
    validator: "NONE",
    placeholder: "[redacted:PERSON_NAME]",
    phase: "PRE_PII",
    defaultOn: true,
  },
  {
    ruleId: "email",
    category: "EMAIL",
    pattern: new RegExp("[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}", "g"),
    validator: "NONE",
    placeholder: "[redacted:EMAIL]",
    phase: "CONTACT",
    defaultOn: true,
  },
  {
    ruleId: "phone",
    category: "PHONE",
    pattern: new RegExp("(?<![\\d\\-])(?:\\+\\d{1,3}[ .\\-]?)?(?:\\(\\d{3}\\)[ .\\-]?|\\d{3}[ .\\-])\\d{3}[ .\\-]?\\d{4}(?![\\d\\-])", "g"),
    validator: "NONE",
    placeholder: "[redacted:PHONE]",
    phase: "CONTACT",
    defaultOn: true,
  },
  {
    ruleId: "account-like-digits",
    category: "ACCOUNT_LIKE",
    pattern: new RegExp("(?<![\\d])(?<![\\d][ .\\-])(?!\\d{8}-\\d{4}-\\d{4}-\\d{4}-\\d{12}(?![\\d\\-]))(?=(?:\\d[ \\-]?){12,})\\d{4,}(?:[ \\-]\\d{4,})+(?![\\d\\-])", "g"),
    validator: "NONE",
    placeholder: "[redacted:ACCOUNT_LIKE]",
    phase: "POST_PII",
    defaultOn: true,
  },
];

/** Every category any rule can report, for a policy that switches one off by name. */
export const CATEGORIES: ReadonlySet<string> = new Set(RULES.map((rule) => rule.category));

/** Every placeholder this rule set writes, so a second pass over a redacted document is a no-op. */
export const PLACEHOLDERS: ReadonlySet<string> = new Set(RULES.map((rule) => rule.placeholder));
