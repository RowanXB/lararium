import { expect, it } from "vitest";

import type { AccountOut, AccountTypeOut } from "../api";
import { byInstitution, statementFormats, tileDetail, typeOf } from "./accountTypes";

const HISTORY_CSV = { label: "History CSV", format: "csv", recommended: false, config: null };

function type(overrides: Partial<AccountTypeOut>): AccountTypeOut {
  return {
    key: "example",
    institution: "Example",
    label: "",
    name: "Example",
    shape: null,
    api: null,
    files: [],
    funding: [],
    family: null,
    ...overrides,
  };
}

const INVEST = type({
  key: "trading212.invest",
  institution: "Trading 212",
  label: "Invest",
  name: "Trading 212 Invest",
  api: { connector: "trading212", config: { account_kind: "invest" } },
  files: [HISTORY_CSV],
});
const MONZO = type({ key: "monzo", institution: "Monzo", name: "Monzo", api: { connector: "monzo", config: {} } });
const PDF_CONFIG = { format: "pdf" as const, layout: "hsbc_uk_statement" as const };
const STATEMENT = type({
  key: "example_bank",
  institution: "Example Bank",
  name: "Example Bank",
  shape: { ledger_type: null, subtype: "bank", tax_wrapper: "none", balance_mode: "transactions" },
  files: [
    { label: "OFX", format: "ofx", recommended: false, config: { format: "ofx" } },
    { label: "PDF statement", format: "pdf", recommended: true, config: PDF_CONFIG },
  ],
});
const CASH_ISA = type({
  key: "trading212.cash_isa",
  institution: "Trading 212",
  label: "Cash ISA",
  name: "Trading 212 Cash ISA",
  shape: { ledger_type: "asset", subtype: "savings_pot", tax_wrapper: "isa", balance_mode: "transactions" },
  files: [{ ...HISTORY_CSV, recommended: true }],
});

it("says how each type's activity arrives", () => {
  expect(tileDetail(INVEST)).toBe("Syncs automatically · CSV export optional");
  expect(tileDetail(MONZO)).toBe("Syncs automatically");
  expect(tileDetail(STATEMENT)).toBe("Upload statements · PDF statement recommended");
  expect(tileDetail(CASH_ISA)).toBe("Upload its History CSV");
});

it("groups types by institution in catalog order", () => {
  expect(byInstitution([INVEST, MONZO, CASH_ISA])).toEqual([
    ["Trading 212", [INVEST, CASH_ISA]],
    ["Monzo", [MONZO]],
  ]);
});

it("gives a type's importable files as catalog formats, the recommended one first", () => {
  expect(statementFormats(STATEMENT)).toEqual([
    {
      institution: "Example Bank",
      label: "PDF statement",
      format: "pdf",
      config: PDF_CONFIG,
      recommended: true,
    },
    {
      institution: "Example Bank",
      label: "OFX",
      format: "ofx",
      config: { format: "ofx" },
      recommended: false,
    },
  ]);
  // A file a connection reads (no profile) is no statement format.
  expect(statementFormats(INVEST)).toEqual([]);
});

it("finds an account's type, and none for a key the catalog no longer has", () => {
  const account = (type_key: string | null) => ({ type_key }) as AccountOut;
  const types = [INVEST, MONZO];
  expect(typeOf(types, account("monzo"))).toBe(MONZO);
  expect(typeOf(types, account("gone"))).toBeUndefined();
  expect(typeOf(types, account(null))).toBeUndefined();
});
