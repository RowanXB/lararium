import { describe, expect, it } from "vitest";
import { formatMoney, formatQuantity, isDecimalString, negateDecimalString } from "./format";

describe("formatMoney", () => {
  it("formats GBP from a string", () => {
    expect(formatMoney("1234567.891", "GBP")).toBe("£1,234,567.89");
  });

  it("formats derived values with long trailing zeros", () => {
    expect(formatMoney("80.00000000000000000000", "GBP")).toBe("£80.00");
  });

  it("keeps precision beyond what a JavaScript number can hold", () => {
    expect(formatMoney("12345678901234567.89", "GBP")).toBe("£12,345,678,901,234,567.89");
  });
});

describe("formatQuantity", () => {
  it("keeps 18 decimals", () => {
    expect(formatQuantity("0.000000000000000001", 18)).toBe("0.000000000000000001");
  });

  it("drops trailing zeros", () => {
    expect(formatQuantity("1500.50000000", 8)).toBe("1,500.5");
  });
});

describe("negateDecimalString", () => {
  it.each([
    ["500", "-500"],
    ["-12.30", "12.30"],
    ["0", "0"],
    ["0.00", "0.00"],
  ])("%s -> %s", (input, expected) => {
    expect(negateDecimalString(input)).toBe(expected);
  });
});

describe("isDecimalString", () => {
  it.each(["0", "1234.56", "-12.30", "000.5"])("accepts %s", (value) => {
    expect(isDecimalString(value)).toBe(true);
  });

  it.each(["", "-", "1.", ".5", "+1", "1e3", "1,000", "NaN", "\u0661\u0662"])(
    "rejects %s",
    (value) => {
      expect(isDecimalString(value)).toBe(false);
    },
  );

  it("rejects a sign when only unsigned amounts are allowed", () => {
    expect(isDecimalString("-500", { unsigned: true })).toBe(false);
    expect(isDecimalString("500", { unsigned: true })).toBe(true);
  });
});
