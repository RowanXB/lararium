import { X } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getFinanceSettings, listAccounts, setTransactionCategories } from "../api";
import { categoryLabel } from "../lib/category";
import { errorMessage } from "../lib/errors";
import { formatMoney, formatQuantity, sumDecimalStrings } from "../lib/format";
import { bucketFor, periodLabel } from "../lib/range";
import { refreshAfterCategoryWrite } from "../lib/refresh";
import { CHART } from "../lib/theme";
import { type Share, ShareChart } from "./charts/ShareChart";
import { CategoryPicker } from "./CategoryPicker";
import { LabelledSelect } from "./LabelledSelect";
import { choiceFrom, RangePicker, type RangeSearch } from "./RangePicker";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Checkbox } from "./ui/checkbox";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "./ui/table";

// What the Income and Spending pages have in common: the range and account, the totals by
// category and by period, and the entries with the owner's categories on them. A page says
// what is its own (the query, the words, how an entry's source shows, which categories to
// offer). Any entry or report with these fields will do, so each page keeps its API types.
export type ReportEntry = {
  transaction_id: string;
  booking_date: string;
  account_name: string | null;
  counterparty_name: string | null;
  description: string;
  amount: string | null;
  quantity: string;
  instrument: string;
  owner_category: string | null;
  source_category: string | null;
};

export type Report<E extends ReportEntry> = {
  total: string;
  missing_count: number;
  bucket: "day" | "week" | "month";
  periods: { start: string; total: string }[];
  categories: { category: string | null; amount: string }[];
  entries: E[];
  known_categories: string[];
};

export type ReportQuery = {
  start: string;
  end: string;
  bucket: "day" | "week" | "month";
  account_id?: string;
  category?: string;
};

// The most transactions one category write takes (TransactionCategoriesUpdate in the API).
const WRITE_LIMIT = 500;
// Instruments allow up to 30 decimals; Intl drops the trailing zeros.
const MAX_DECIMALS = 30;
const ALL_ACCOUNTS = "all";
// What the API takes to mean "no category at all".
const UNCATEGORISED = "uncategorised";

function percent(part: string, whole: string): number {
  const total = Number(whole);
  return total > 0 ? (Number(part) / total) * 100 : 0;
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}

type CategoriesProps = {
  report: Report<ReportEntry>;
  currency: string;
  selected: string | null;
  onSelect: (category: string | null) => void;
  // "No income in these dates" and the chart's label, from the page's noun.
  emptyText: string;
  chartLabel: string;
};

// The categories are buttons: one click narrows the entries to it, a second clears that.
function ByCategory({ report, currency, selected, onSelect, emptyText, chartLabel }: CategoriesProps) {
  const format = useCallback((value: string) => formatMoney(value, currency), [currency]);
  const rows = useMemo(
    () =>
      report.categories.map((c, index) => ({
        key: c.category ?? UNCATEGORISED,
        name: categoryLabel(c.category),
        value: c.amount,
        color: CHART.series[index % CHART.series.length],
      })),
    [report.categories],
  );
  // A category its refunds took below zero has no share of the whole: shares, in the chart
  // and the list alike, are of the positive categories' sum.
  const shares: Share[] = rows.filter((r) => Number(r.value) > 0);
  const whole = sumDecimalStrings(shares.map((share) => share.value));
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyText}</p>;
  }
  return (
    <div className="flex flex-wrap items-start gap-6">
      <ShareChart shares={shares} format={format} label={chartLabel} />
      <ul className="max-h-64 min-w-48 flex-1 space-y-1 overflow-y-auto pr-1">
        {rows.map((row) => (
          <li key={row.key}>
            <button
              type="button"
              aria-pressed={selected === row.key}
              onClick={() => onSelect(selected === row.key ? null : row.key)}
              className="flex w-full items-baseline justify-between gap-3 rounded-md px-2 py-1 text-left text-sm hover:bg-secondary aria-pressed:bg-secondary"
            >
              <span className="flex min-w-0 items-center gap-2">
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ backgroundColor: row.color }}
                  aria-hidden
                />
                <span className="truncate">{row.name}</span>
              </span>
              <span className="shrink-0 tabular-nums">
                {format(row.value)}
                {Number(row.value) > 0 && (
                  <span className="ml-2 text-muted-foreground">
                    {Math.round(percent(row.value, whole))}%
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ByPeriod({ report, currency }: { report: Report<ReportEntry>; currency: string }) {
  return (
    <ul className="max-h-64 space-y-2 overflow-y-auto pr-1">
      {report.periods.map((period) => (
        <li key={period.start} className="flex items-baseline justify-between gap-3 text-sm">
          <span className="text-muted-foreground">{periodLabel(period.start, report.bucket)}</span>
          <span className="tabular-nums">{formatMoney(period.total, currency)}</span>
        </li>
      ))}
    </ul>
  );
}

function AmountCell({
  entry,
  currency,
  note,
}: {
  entry: ReportEntry;
  currency: string;
  note: string | null;
}) {
  const own = entry.instrument === currency;
  return (
    <TableCell className="text-right tabular-nums">
      {entry.amount === null ? (
        <span className="text-warning">No price</span>
      ) : (
        formatMoney(entry.amount, currency)
      )}
      {note && <span className="block text-xs text-muted-foreground">{note}</span>}
      {(!own || entry.amount === null) && (
        <span className="block text-xs text-muted-foreground">
          {formatQuantity(entry.quantity, MAX_DECIMALS)} {entry.instrument}
        </span>
      )}
    </TableCell>
  );
}

type TableProps<E extends ReportEntry> = {
  entries: E[];
  currency: string;
  known: string[];
  suggestions?: readonly string[];
  payeeHeading: string;
  sourceOf: (entry: E) => string;
  noteOf?: (entry: E) => string | null;
  selected: Set<string>;
  onToggle: (ids: string[], on: boolean) => void;
  // Settles when the write does; its error is shown by the page.
  onCategory: (ids: string[], category: string | null) => Promise<unknown>;
};

function EntriesTable<E extends ReportEntry>({
  entries,
  currency,
  known,
  suggestions,
  payeeHeading,
  sourceOf,
  noteOf,
  selected,
  onToggle,
  onCategory,
}: TableProps<E>) {
  const all = entries.length > 0 && entries.every((e) => selected.has(e.transaction_id));
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-8">
            <Checkbox
              aria-label="Select all"
              checked={all}
              onCheckedChange={(on) => onToggle(entries.map((e) => e.transaction_id), on === true)}
            />
          </TableHead>
          <TableHead>Date</TableHead>
          <TableHead>Account</TableHead>
          <TableHead>{payeeHeading}</TableHead>
          <TableHead className="text-right">Amount</TableHead>
          <TableHead>Source</TableHead>
          <TableHead>Category</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.map((entry) => {
          const id = entry.transaction_id;
          return (
            <TableRow key={`${id}-${entry.instrument}`}>
              <TableCell>
                <Checkbox
                  aria-label={`Select ${entry.description}`}
                  checked={selected.has(id)}
                  onCheckedChange={(on) => onToggle([id], on === true)}
                />
              </TableCell>
              <TableCell className="whitespace-nowrap">{entry.booking_date}</TableCell>
              <TableCell>{entry.account_name ?? "—"}</TableCell>
              <TableCell>
                {entry.counterparty_name && <span className="block">{entry.counterparty_name}</span>}
                <span className="block text-muted-foreground">{entry.description}</span>
              </TableCell>
              <AmountCell entry={entry} currency={currency} note={noteOf?.(entry) ?? null} />
              {/* The source's own mark, as it came: the owner's category never replaces it. */}
              <TableCell>{sourceOf(entry)}</TableCell>
              <TableCell>
                <div className="flex items-center gap-1">
                  <CategoryPicker
                    label={`Category for ${entry.description}`}
                    value={entry.owner_category}
                    known={known}
                    suggestions={suggestions}
                    onChange={(category) => onCategory([id], category)}
                  />
                  {entry.owner_category !== null && (
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label={`Clear category for ${entry.description}`}
                      onClick={() => void onCategory([id], null).catch(() => undefined)}
                    >
                      <X aria-hidden />
                    </Button>
                  )}
                </div>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

type Props<E extends ReportEntry, R extends Report<E>> = {
  title: string;
  // "income" or "spending": the first part of the report's query key and of its empty text.
  noun: string;
  fetchReport: (query: ReportQuery) => Promise<R>;
  payeeHeading: string;
  sourceOf: (entry: E) => string;
  noteOf?: (entry: E) => string | null;
  // The categories the pickers offer; by default the report's known ones.
  offered?: (report: R) => string[];
  suggestions?: readonly string[];
  // The dates a link opened the page on (the Overview's), else the default.
  initialRange?: RangeSearch;
};

export function CategoryReport<E extends ReportEntry, R extends Report<E>>({
  title,
  noun,
  fetchReport,
  payeeHeading,
  sourceOf,
  noteOf,
  offered,
  suggestions,
  initialRange = {},
}: Props<E, R>) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState(() => choiceFrom(initialRange));
  const [accountId, setAccountId] = useState(ALL_ACCOUNTS);
  const [category, setCategory] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<string | null>(null);
  const { start, end } = choice.range;
  const bucket = bucketFor(choice.range);

  const settings = useQuery({
    queryKey: ["finance-settings"],
    queryFn: async () => (await getFinanceSettings({ throwOnError: true })).data,
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });
  const report = useQuery({
    queryKey: [noun, start, end, bucket, accountId, category],
    queryFn: () =>
      fetchReport({
        start,
        end,
        bucket,
        ...(accountId !== ALL_ACCOUNTS && { account_id: accountId }),
        ...(category !== null && { category }),
      }),
  });

  const write = useMutation({
    mutationFn: async (v: { ids: string[]; category: string | null; bulk?: boolean }) => {
      // The API takes at most WRITE_LIMIT ids a request, each request all or nothing; a
      // year of daily rewards selected at once is more, so it goes in turn.
      for (let at = 0; at < v.ids.length; at += WRITE_LIMIT) {
        await setTransactionCategories({
          body: { transaction_ids: v.ids.slice(at, at + WRITE_LIMIT), category: v.category },
          throwOnError: true,
        });
      }
    },
    onSuccess: (_, v) => {
      // A single row's write leaves a bulk selection in progress alone.
      if (v.bulk) {
        setSelected(new Set());
        setBulk(null);
      }
    },
    // After a failure too: the requests before it were written, and the page shows them.
    onSettled: () => refreshAfterCategoryWrite(queryClient),
  });

  // What the selection was made on is gone when the view changes; never act on rows unseen.
  useEffect(() => {
    setSelected(new Set());
    setBulk(null);
  }, [start, end, accountId, category]);

  const toggle = (ids: string[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });

  const currency = settings.data?.reporting_currency;
  const data = report.data;
  // A refetch can drop rows: only the ones still shown count and are sent.
  const shownIds = useMemo(() => new Set((data?.entries ?? []).map((e) => e.transaction_id)), [data]);
  const chosen = useMemo(() => [...selected].filter((id) => shownIds.has(id)), [selected, shownIds]);
  // An account left out of reports has no entries here, so it is no filter either.
  const own = (accounts.data ?? []).filter((a) => !a.system && !a.excluded_from_reports);
  const accountOptions = [
    { value: ALL_ACCOUNTS, label: "All accounts" },
    ...own.map((a) => ({ value: a.id, label: a.name })),
  ];
  const uncategorised = data?.categories.find((c) => c.category === null)?.amount ?? "0";
  const error = settings.error ?? accounts.error ?? report.error ?? write.error;
  const known = useMemo(
    () => (data ? (offered ? offered(data) : data.known_categories) : []),
    [data, offered],
  );
  const emptyText = `No ${noun} in these dates`;
  const noun1 = noun.charAt(0).toUpperCase() + noun.slice(1);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        <div className="flex flex-wrap items-end gap-3">
          <RangePicker choice={choice} onChange={setChoice} />
          <LabelledSelect
            label="Account"
            value={accountId}
            options={accountOptions}
            onChange={setAccountId}
          />
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}

      {data && currency && (
        <>
          <div className="flex flex-wrap gap-10">
            <Figure label="Total" value={formatMoney(data.total, currency)} />
            <Figure label="Uncategorised" value={formatMoney(uncategorised, currency)} />
          </div>
          {data.missing_count > 0 && (
            <p className="text-sm text-warning">
              {data.missing_count === 1
                ? "1 entry has no price or FX rate and is left out of the totals"
                : `${data.missing_count} entries have no price or FX rate and are left out of the totals`}
            </p>
          )}

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>By category</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {category !== null && (
                  <p className="flex items-center gap-2 text-sm">
                    <span>
                      Showing {category === UNCATEGORISED ? "Uncategorised" : categoryLabel(category)}
                    </span>
                    <Button size="sm" variant="outline" onClick={() => setCategory(null)}>
                      Clear filter
                    </Button>
                  </p>
                )}
                <ByCategory
                  report={data}
                  currency={currency}
                  selected={category}
                  onSelect={setCategory}
                  emptyText={emptyText}
                  chartLabel={`${noun1} by category`}
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>By {bucket}</CardTitle>
              </CardHeader>
              <CardContent>
                <ByPeriod report={data} currency={currency} />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardContent className="space-y-4">
              {chosen.length > 0 && (
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <span aria-live="polite">{chosen.length} selected</span>
                  <CategoryPicker
                    label="Category for selected"
                    value={bulk}
                    known={known}
                    suggestions={suggestions}
                    onChange={setBulk}
                  />
                  <Button
                    size="sm"
                    disabled={bulk === null || write.isPending}
                    onClick={() => write.mutate({ ids: chosen, category: bulk, bulk: true })}
                  >
                    Apply
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={write.isPending}
                    onClick={() => write.mutate({ ids: chosen, category: null, bulk: true })}
                  >
                    Clear category
                  </Button>
                </div>
              )}
              {data.entries.length === 0 ? (
                <p className="text-sm text-muted-foreground">{emptyText}</p>
              ) : (
                <EntriesTable
                  entries={data.entries}
                  currency={currency}
                  known={known}
                  suggestions={suggestions}
                  payeeHeading={payeeHeading}
                  sourceOf={sourceOf}
                  noteOf={noteOf}
                  selected={new Set(chosen)}
                  onToggle={toggle}
                  onCategory={(ids, next) => write.mutateAsync({ ids, category: next })}
                />
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
