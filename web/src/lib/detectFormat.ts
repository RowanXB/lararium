import type { ImportCatalogEntryOut } from "../api";

// Which of one bank's formats a file is, from its extension and, for CSV, its first line.
// Within one bank the extension almost always settles it; where it doesn't (HSBC's two
// CSVs), the columns a format names must all be in the file's header row, and a format
// that supplies its own header reads a file whose first line is a transaction.

const EXTENSIONS: Record<string, string> = { csv: "csv", ofx: "ofx", qfx: "ofx", qif: "qif", pdf: "pdf" };

export function fileFormat(fileName: string): string | undefined {
  const extension = /\.([a-z0-9]+)$/i.exec(fileName)?.[1]?.toLowerCase();
  return extension ? EXTENSIONS[extension] : undefined;
}

function headerCells(firstLine: string): string[] {
  return firstLine
    .replace(/^﻿/, "")
    .split(",")
    .map((cell) => cell.trim().replace(/^"(.*)"$/, "$1").trim());
}

function columnNames(entry: ImportCatalogEntryOut): string[] {
  if (entry.config.format !== "csv") return [];
  return Object.values(entry.config.columns).flatMap((value) =>
    value === null || value === undefined ? [] : Array.isArray(value) ? value : [String(value)],
  );
}

export type Detection =
  | { kind: "found"; entry: ImportCatalogEntryOut }
  | { kind: "ambiguous"; entries: ImportCatalogEntryOut[] }
  | { kind: "unknown" }; // none of the bank's formats has this extension

export function detectFormat(
  formats: ImportCatalogEntryOut[],
  fileName: string,
  firstLine = "",
): Detection {
  const format = fileFormat(fileName);
  const candidates = formats.filter((entry) => entry.config.format === format);
  if (candidates.length === 0) return { kind: "unknown" };
  if (candidates.length === 1) return { kind: "found", entry: candidates[0] };
  const cells = new Set(headerCells(firstLine));
  const named = candidates.filter(
    (entry) =>
      entry.config.format === "csv" &&
      !entry.config.header &&
      columnNames(entry).every((name) => cells.has(name)),
  );
  if (named.length === 1) return { kind: "found", entry: named[0] };
  const headerless = candidates.filter(
    (entry) => entry.config.format === "csv" && Boolean(entry.config.header),
  );
  if (named.length === 0 && headerless.length === 1) return { kind: "found", entry: headerless[0] };
  return { kind: "ambiguous", entries: candidates };
}

// The first line of a text file, read from its start only.
export async function firstLineOf(file: File): Promise<string> {
  const head = await file.slice(0, 8192).text();
  return head.split(/\r?\n/, 1)[0] ?? "";
}
