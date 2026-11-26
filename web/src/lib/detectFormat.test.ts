import { expect, it } from "vitest";

import type { ImportCatalogEntryOut } from "../api";
import { detectFormat, fileFormat } from "./detectFormat";

function entry(label: string, config: Record<string, unknown>): ImportCatalogEntryOut {
  return { institution: "HSBC UK", label, recommended: false, config } as never;
}

const PDF = entry("PDF statement", { format: "pdf", layout: "hsbc_uk_statement" });
const OFX = entry("OFX", { format: "ofx" });
const MIDATA = entry("midata CSV", {
  format: "csv",
  columns: {
    date: "Date",
    type: "Type",
    description: ["Merchant/Description"],
    amount: "Debit/Credit",
    balance: "Balance",
  },
});
const LIST = entry("CSV (transaction list)", {
  format: "csv",
  header: ["Date", "Description", "Amount"],
  columns: { date: "Date", description: ["Description"], amount: "Amount" },
});
const HSBC = [PDF, MIDATA, OFX, LIST];

it("reads the format from the extension", () => {
  expect(fileFormat("Statement.PDF")).toBe("pdf");
  expect(fileFormat("export.qfx")).toBe("ofx");
  expect(fileFormat("notes.txt")).toBeUndefined();
  expect(detectFormat(HSBC, "2026-11.pdf")).toEqual({ kind: "found", entry: PDF });
  expect(detectFormat(HSBC, "2026-11.qfx")).toEqual({ kind: "found", entry: OFX });
});

it("tells two CSVs of one bank apart by their first line", () => {
  expect(
    detectFormat(HSBC, "midata.csv", '﻿"Date","Type","Merchant/Description","Debit/Credit","Balance"'),
  ).toEqual({ kind: "found", entry: MIDATA });
  // The transaction list has no header row: its first line is a payment.
  expect(detectFormat(HSBC, "list.csv", "03/11/2026,CORNER CAFE,-4.50")).toEqual({
    kind: "found",
    entry: LIST,
  });
});

it("says when it can't tell, or when the bank has no such format", () => {
  const twoHeaderless = [LIST, { ...LIST, label: "Other list" }];
  expect(detectFormat(twoHeaderless, "x.csv", "a,b,c")).toEqual({
    kind: "ambiguous",
    entries: twoHeaderless,
  });
  expect(detectFormat([PDF], "x.csv")).toEqual({ kind: "unknown" });
});
