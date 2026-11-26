// Amounts arrive as decimal strings. Intl.NumberFormat accepts them directly and
// keeps their full precision, so they never pass through a JavaScript number here.

export function formatMoney(value: string, currency: string, locale = "en-GB"): string {
  return new Intl.NumberFormat(locale, { style: "currency", currency }).format(
    value as Intl.StringNumericLiteral,
  );
}

export function formatQuantity(value: string, maxDecimals: number, locale = "en-GB"): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxDecimals,
  }).format(value as Intl.StringNumericLiteral);
}

export function negateDecimalString(value: string): string {
  if (value.startsWith("-")) return value.slice(1);
  if (/^[0.]*$/.test(value)) return value;
  return `-${value}`;
}

// The backend's DecimalStr pattern (ASCII digits only, at most 40 on each side of the
// point), optionally without the sign.
const DECIMAL = /^-?[0-9]{1,40}(\.[0-9]{1,40})?$/;
const UNSIGNED_DECIMAL = /^[0-9]{1,40}(\.[0-9]{1,40})?$/;

// "0", "-0.00" and the like: a decimal string that is zero, read without a float.
export function isZeroDecimal(value: string): boolean {
  return /^-?0*(\.0*)?$/.test(value) && /[0-9]/.test(value);
}

export function isDecimalString(value: string, { unsigned = false } = {}): boolean {
  return (unsigned ? UNSIGNED_DECIMAL : DECIMAL).test(value);
}

// "112233" → "11-22-33"; anything else is shown as it is.
export function formatSortCode(sortCode: string): string {
  const parts = /^([0-9]{2})([0-9]{2})([0-9]{2})$/.exec(sortCode);
  return parts ? `${parts[1]}-${parts[2]}-${parts[3]}` : sortCode;
}

// Whole units for a chart axis or a headline: "£12,346", "-£50".
export function formatMoneyShort(value: number, currency: string, locale = "en-GB"): string {
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    maximumFractionDigits: 0,
    notation: Math.abs(value) >= 100_000 ? "compact" : "standard",
  }).format(value);
}

// The sum of decimal strings, exactly: amounts never pass through a float.
export function sumDecimalStrings(values: string[]): string {
  const decimals = (value: string) => value.split(".")[1]?.length ?? 0;
  const scale = Math.max(0, ...values.map(decimals));
  const scaled = (value: string) => {
    const [whole, fraction = ""] = value.replace("-", "").split(".");
    const digits = BigInt(whole + fraction.padEnd(scale, "0"));
    return value.startsWith("-") ? -digits : digits;
  };
  const total = values.reduce((sum, value) => sum + scaled(value), BigInt(0));
  const negative = total < BigInt(0);
  const digits = (negative ? -total : total).toString().padStart(scale + 1, "0");
  const text = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
  return negative ? `-${text}` : text;
}

export function subtractDecimalStrings(a: string, b: string): string {
  return sumDecimalStrings([a, negateDecimalString(b)]);
}
