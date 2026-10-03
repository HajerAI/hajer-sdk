/**
 * A shape says *could be*; the checksum says *is*.
 *
 * The same checksums the Python SDK carries and the Hajer platform declares, so that this client
 * removes exactly what the other two would: a client Luhn that disagreed with the server's would
 * report a `clientRedaction` count that does not describe what it sent.
 *
 * They never see a document. Each is handed the text one pattern matched and answers true or false;
 * none throws — a value that cannot be read as the shape claims is `false`, because a detector that
 * threw on a malformed candidate would break a customer's own request path over a number in a
 * comment.
 *
 * Hand-written rather than generated: the Python copy is generated because the backend module is
 * Python and a generator can copy Python to Python, and three functions of twelve lines each are
 * cheaper to read here than a second generator would be. `test/redact.test.ts` pins them against the
 * same vectors, which is what actually keeps them honest.
 */

const VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2] as const;
const VIN_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ";
const VIN_VALUES = [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9] as const;
const VIN_LETTER_VALUE = new Map<string, number>(
  [...VIN_LETTERS].map((letter, index) => [letter, VIN_VALUES[index]]),
);
const VIN_CHECK_POSITION = 8;
const IBAN_REMAINDER = 1;
const IBAN_ROTATION = 4;
const IBAN_LETTER_OFFSET = 55;

/** What one validator is: the matched text in, "this really is one of those" out. */
export type Validator = (text: string) => boolean;

function digitsOf(text: string): number[] {
  const found: number[] = [];
  for (const character of text) {
    if (character >= "0" && character <= "9") found.push(Number(character));
  }
  return found;
}

/**
 * The Luhn sum over the digits of `text`, ignoring the separators between them.
 *
 * Separators are skipped rather than refused because the catalog's own patterns admit them: a card
 * is written in groups of four as often as it is written whole, and the checksum is over the digits
 * either way. An empty candidate is false — a checksum over nothing proves nothing.
 */
export function luhn(text: string): boolean {
  const digits = digitsOf(text);
  if (digits.length === 0) return false;
  let total = 0;
  for (let index = digits.length - 1; index >= 0; index -= 1) {
    const fromEnd = digits.length - 1 - index;
    const digit = digits[index];
    if (fromEnd % 2 === 0) {
      total += digit;
    } else {
      const doubled = digit * 2;
      total += Math.floor(doubled / 10) + (doubled % 10);
    }
  }
  return total % 10 === 0;
}

/** How many digits a payment card has: ISO/IEC 7812 allows 13 to 19. */
const CARD_MIN_DIGITS = 13;
const CARD_MAX_DIGITS = 19;

/**
 * A card written with more digits after it (its expiry, its CVV, a second card): the run begins with a
 * 13 to 19 digit number that passes Luhn. The rule naming this validator removes the whole run.
 */
export function luhnPrefix(text: string): boolean {
  const digits = [...text].filter((character) => character >= "0" && character <= "9").join("");
  for (let length = CARD_MIN_DIGITS; length <= Math.min(CARD_MAX_DIGITS, digits.length); length += 1) {
    if (luhn(digits.slice(0, length))) return true;
  }
  return false;
}

/**
 * ISO 13616: move the first four characters to the end, letters become numbers, mod 97 is 1.
 *
 * The modulus is taken digit by digit rather than over one big integer: an IBAN is up to 34
 * characters and becomes a ~60-digit number, which is past what a double can hold exactly.
 */
export function ibanMod97(text: string): boolean {
  const packed = text.replace(/\s+/gu, "").toUpperCase();
  if (packed.length <= IBAN_ROTATION) return false;
  const rotated = packed.slice(IBAN_ROTATION) + packed.slice(0, IBAN_ROTATION);
  let numeric = "";
  for (const character of rotated) {
    if (character >= "A" && character <= "Z") {
      numeric += String(character.charCodeAt(0) - IBAN_LETTER_OFFSET);
    } else if (character >= "0" && character <= "9") {
      numeric += character;
    } else {
      return false;
    }
  }
  let remainder = 0;
  for (const digit of numeric) {
    remainder = (remainder * 10 + Number(digit)) % 97;
  }
  return remainder === IBAN_REMAINDER;
}

/**
 * ISO 3779: the ninth character is the weighted sum of all seventeen, modulo eleven, with X for ten.
 *
 * The VIN alphabet has no I, O or Q — the pattern already excludes them, and a character outside the
 * table is false here as well, so the two cannot disagree. A candidate of the wrong length is false.
 */
export function vinCheck(text: string): boolean {
  if (text.length !== VIN_WEIGHTS.length) return false;
  let total = 0;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    const value =
      character >= "0" && character <= "9" ? Number(character) : VIN_LETTER_VALUE.get(character);
    if (value === undefined) return false;
    total += value * VIN_WEIGHTS[index];
  }
  const remainder = total % 11;
  return text[VIN_CHECK_POSITION] === (remainder === 10 ? "X" : String(remainder));
}

/**
 * The absence of a checksum: a rule whose shape is its whole argument admits every match.
 *
 * A function rather than a `null` in the table, so the walk has one shape to call and no branch on
 * whether a rule has a validator at all.
 */
function none(text: string): boolean {
  return text.length > 0;
}

/**
 * Every validator a generated rule may name. A rule naming anything else degrades the pass and
 * records it rather than throwing — `redact.ts` is the one place that decides what that means.
 */
export const VALIDATORS: Readonly<Record<string, Validator>> = Object.freeze({
  NONE: none,
  LUHN: luhn,
  LUHN_PREFIX: luhnPrefix,
  IBAN_MOD97: ibanMod97,
  VIN_CHECK: vinCheck,
});
