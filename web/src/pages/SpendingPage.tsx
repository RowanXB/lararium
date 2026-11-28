import { getSpending, type SpendingEntryOut, type SpendingOut } from "../api";
import { CategoryReport } from "../components/CategoryReport";
import type { RangeSearch } from "../components/RangePicker";
import { categoryLabel } from "../lib/category";

// The source's own mark, as it came: the owner's category never replaces it.
function sourceOf(entry: SpendingEntryOut): string {
  return entry.source_category ? categoryLabel(entry.source_category) : "—";
}

// The categories in use on this side, then the source's words for the entries listed that
// are not among them, so the owner can reuse them (spec 3.3).
function offered(report: SpendingOut): string[] {
  const known = new Set(report.known_categories);
  const fromSource = new Set<string>();
  for (const entry of report.entries) {
    if (entry.source_category && !known.has(entry.source_category)) {
      fromSource.add(entry.source_category);
    }
  }
  return [...report.known_categories, ...fromSource];
}

const NO_SUGGESTIONS: readonly string[] = [];

// A move to a hidden account is never a refund, so at most one note applies.
function noteOf(entry: SpendingEntryOut): string | null {
  if (entry.hidden_account_name) return `To ${entry.hidden_account_name}`;
  return entry.refund ? "Refund" : null;
}

// `initialRange`: the dates a link opened the page on (the Overview's), else its default.
export function SpendingPage({ initialRange = {} }: { initialRange?: RangeSearch }) {
  return (
    <CategoryReport<SpendingEntryOut, SpendingOut>
      title="Spending"
      noun="spending"
      fetchReport={async (query) => (await getSpending({ query, throwOnError: true })).data}
      payeeHeading="To / description"
      sourceOf={sourceOf}
      noteOf={noteOf}
      offered={offered}
      suggestions={NO_SUGGESTIONS}
      initialRange={initialRange}
    />
  );
}
