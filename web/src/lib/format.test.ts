import { describe, expect, it } from "vitest";
import { formatMoney, formatQuantity, negateDecimalString } from "./format";

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
