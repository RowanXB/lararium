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

// The backend's DecimalStr pattern (ASCII digits only), optionally without the sign.
const DECIMAL = /^-?[0-9]+(\.[0-9]+)?$/;
const UNSIGNED_DECIMAL = /^[0-9]+(\.[0-9]+)?$/;

export function isDecimalString(value: string, { unsigned = false } = {}): boolean {
  return (unsigned ? UNSIGNED_DECIMAL : DECIMAL).test(value);
}
