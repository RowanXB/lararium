import { getIncome, type IncomeEntryOut, type IncomeOut } from "../api";
import { CategoryReport } from "../components/CategoryReport";
import type { RangeSearch } from "../components/RangePicker";
import { categoryLabel, incomeKindLabel } from "../lib/category";

// The source's own mark, as it came: the owner's category never replaces it.
function sourceOf(entry: IncomeEntryOut): string {
  if (entry.income_kind) return incomeKindLabel(entry.income_kind);
  return entry.source_category ? categoryLabel(entry.source_category) : "—";
}

// Money moved in from a hidden account, which counts as someone else's.
function noteOf(entry: IncomeEntryOut): string | null {
  return entry.hidden_account_name ? `From ${entry.hidden_account_name}` : null;
}

// `initialRange`: the dates a link opened the page on (the Overview's), else its default.
export function IncomePage({ initialRange = {} }: { initialRange?: RangeSearch }) {
  return (
    <CategoryReport<IncomeEntryOut, IncomeOut>
      title="Income"
      noun="income"
      fetchReport={async (query) => (await getIncome({ query, throwOnError: true })).data}
      payeeHeading="From / description"
      sourceOf={sourceOf}
      noteOf={noteOf}
      initialRange={initialRange}
    />
  );
}
