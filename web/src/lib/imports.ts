import type { AccountOut, ImportBatchOut, InstrumentOut } from "../api";

export const BATCH_STATUS_LABELS: Record<ImportBatchOut["status"], string> = {
  staged: "Staged",
  committed: "Committed",
  discarded: "Discarded",
  rolled_back: "Rolled back",
};

// Row statuses in the order they're worth reading.
const ROW_STATUSES = ["new", "committed", "duplicate", "skipped", "error", "rolled_back"] as const;

export const ROW_STATUS_LABELS: Record<(typeof ROW_STATUSES)[number], string> = {
  new: "New",
  committed: "Committed",
  duplicate: "Duplicate",
  skipped: "Skipped",
  error: "Error",
  rolled_back: "Rolled back",
};

// "12 committed · 1 duplicate": the non-zero counts, statuses the UI doesn't know last.
export function countsSummary(counts: Record<string, number>): string {
  const known: readonly string[] = ROW_STATUSES;
  const keys = [
    ...ROW_STATUSES.filter((status) => status in counts),
    ...Object.keys(counts).filter((status) => !known.includes(status)),
  ];
  return keys
    .filter((status) => (counts[status] ?? 0) > 0)
    .map((status) => `${counts[status]} ${status.replace("_", " ")}`)
    .join(" · ");
}

// Valuation ignores transactions on snapshots accounts, so only these accept imports.
export function isImportable(account: AccountOut): boolean {
  return (
    account.status === "open" &&
    account.source_kind === "manual" &&
    (account.balance_mode === "transactions" || account.balance_mode === "hybrid")
  );
}

export function isoCode(instrument: InstrumentOut): string {
  return (
    instrument.identifiers.find((identifier) => identifier.scheme === "iso4217")?.value ??
    instrument.symbol
  );
}

export function formatTimestamp(value: string, locale = "en-GB"): string {
  return new Date(value).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}
