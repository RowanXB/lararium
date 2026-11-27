import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowDownRight, ArrowUpRight, Plus } from "lucide-react";
import { Fragment, type ReactNode, useCallback, useMemo, useState } from "react";

import {
  type CashFlowOut,
  getCashFlow,
  getFinanceSettings,
  getHoldings,
  getNetWorth,
  type HoldingOut,
  listAccounts,
  listTransfers,
} from "../api";
import { CashFlowChart } from "../components/charts/CashFlowChart";
import { NetWorthChart } from "../components/charts/NetWorthChart";
import { type Share, ShareChart } from "../components/charts/ShareChart";
import { initialChoice, RangePicker } from "../components/RangePicker";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { categoryLabel } from "../lib/category";
import { errorMessage } from "../lib/errors";
import {
  formatMoney,
  formatQuantity,
  isZeroDecimal,
  subtractDecimalStrings,
  sumDecimalStrings,
} from "../lib/format";
import { bucketFor, dayLabel } from "../lib/range";
import { CHART } from "../lib/theme";
import { needsAttention } from "./TransfersPage";

// Instruments allow up to 30 decimals; Intl drops the trailing zeros.
const MAX_DECIMALS = 30;

const STATUS_LABELS: Record<HoldingOut["status"], string> = {
  ok: "OK",
  missing_price: "Missing price",
  missing_fx: "Missing FX rate",
};

function missingMessage(count: number): string {
  return count === 1
    ? "1 item is missing a price or FX rate and is excluded from net worth"
    : `${count} items are missing a price or FX rate and are excluded from net worth`;
}

// The latest day has its own message above; this covers the rest of the chart's range.
function earlierIncompleteMessage(days: number): string {
  return days === 1
    ? "1 earlier day in the chart leaves out items missing a price or FX rate (marked in amber)"
    : `${days} earlier days in the chart leave out items missing a price or FX rate (marked in amber)`;
}

function missingFlowMessage(count: number): string {
  return count === 1
    ? "1 payment is left out of income and spending for lack of an FX rate"
    : `${count} payments are left out of income and spending for lack of an FX rate`;
}

function groupByAccount(holdings: HoldingOut[]): HoldingOut[][] {
  const groups = new Map<string, HoldingOut[]>();
  for (const holding of holdings) {
    const group = groups.get(holding.account_id);
    if (group) group.push(holding);
    else groups.set(holding.account_id, [holding]);
  }
  return [...groups.values()];
}

// A share of a whole as a percentage, for a bar's width or a label: an approximation, so a
// float is fine here; the amounts beside it stay exact strings.
function percent(part: string, whole: string): number {
  const total = Number(whole);
  return total > 0 ? (Number(part) / total) * 100 : 0;
}

function Notice({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      className="space-y-1 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning"
    >
      {children}
    </div>
  );
}

function Figure({ label, value, tone, note }: { label: string; value: string; tone?: string; note?: string }) {
  return (
    <Card>
      <CardContent className="space-y-1">
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className={`text-2xl font-semibold tabular-nums ${tone ?? ""}`}>{value}</p>
        {note && <p className="text-sm text-muted-foreground">{note}</p>}
      </CardContent>
    </Card>
  );
}

// A row with a bar behind its name: how much of the whole it is.
function BarRow({ name, amount, share, color }: { name: string; amount: string; share: number; color: string }) {
  return (
    <li className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="truncate">{name}</span>
        <span className="shrink-0 tabular-nums">{amount}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full"
          style={{ width: `${Math.max(0, Math.min(100, share))}%`, backgroundColor: color }}
        />
      </div>
    </li>
  );
}

function Welcome() {
  return (
    <div className="mx-auto max-w-xl space-y-6 py-16 text-center">
      <div className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">See all your money in one place</h1>
        <p className="text-muted-foreground">
          Connect a bank or broker, upload a statement, or type in a balance.
        </p>
      </div>
      <Link
        to="/accounts/new"
        className="inline-flex h-10 items-center gap-2 rounded-lg bg-primary px-5 font-medium text-primary-foreground hover:bg-primary/85"
      >
        <Plus className="size-4" aria-hidden />
        Add your first account
      </Link>
    </div>
  );
}

function HiddenAccounts({ flow, currency }: { flow: CashFlowOut; currency: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Moved to and from hidden accounts</CardTitle>
        <CardDescription>
          A hidden account counts as someone else's, so this is in income and spending above
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y">
          {flow.hidden_accounts.map((account) => (
            <li
              key={account.account_id}
              className="flex flex-wrap items-baseline justify-between gap-3 py-2 text-sm first:pt-0 last:pb-0"
            >
              <span className="font-medium">{account.name}</span>
              <span className="flex gap-4 tabular-nums">
                {!isZeroDecimal(account.moved_out) && (
                  <span className="text-spending">
                    Moved in {formatMoney(account.moved_out, currency)}
                  </span>
                )}
                {!isZeroDecimal(account.moved_in) && (
                  <span className="text-income">
                    Taken out {formatMoney(account.moved_in, currency)}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function SpendingByCategory({ flow, currency }: { flow: CashFlowOut; currency: string }) {
  const shares: Share[] = useMemo(
    () =>
      flow.categories
        .filter((c) => !isZeroDecimal(c.spending))
        .map((c, index) => ({
          name: categoryLabel(c.category),
          value: c.spending,
          color: CHART.series[index % CHART.series.length],
        })),
    [flow.categories],
  );
  const format = useCallback((value: string) => formatMoney(value, currency), [currency]);
  // Money moved to hidden accounts is spending too, but no category's.
  const categorised = sumDecimalStrings(shares.map((share) => share.value));
  if (shares.length === 0) {
    return <p className="text-sm text-muted-foreground">No spending in these dates</p>;
  }
  return (
    <div className="flex flex-wrap items-start gap-6">
      <ShareChart shares={shares} format={format} label="Spending by category" />
      <ul className="max-h-64 min-w-48 flex-1 space-y-2 overflow-y-auto pr-1">
        {shares.map((share) => (
          <li key={share.name} className="flex items-baseline justify-between gap-3 text-sm">
            <span className="flex min-w-0 items-center gap-2">
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: share.color }}
                aria-hidden
              />
              <span className="truncate">{share.name}</span>
            </span>
            <span className="shrink-0 tabular-nums">
              {format(share.value)}
              <span className="ml-2 text-muted-foreground">
                {Math.round(percent(share.value, categorised))}%
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function OverviewPage() {
  const [choice, setChoice] = useState(initialChoice);
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
  const transfers = useQuery({
    queryKey: ["transfers"],
    queryFn: async () => (await listTransfers({ throwOnError: true })).data,
  });
  const netWorth = useQuery({
    queryKey: ["net-worth", start, end],
    queryFn: async () => (await getNetWorth({ query: { start, end }, throwOnError: true })).data,
  });
  const holdings = useQuery({
    queryKey: ["holdings", end],
    queryFn: async () => (await getHoldings({ query: { date: end }, throwOnError: true })).data,
  });
  const cashFlow = useQuery({
    queryKey: ["cash-flow", start, end, bucket],
    queryFn: async () =>
      (await getCashFlow({ query: { start, end, bucket }, throwOnError: true })).data,
  });

  const currency = settings.data?.reporting_currency;
  const points = netWorth.data ?? [];
  const latest = points.at(-1);
  const earlierIncomplete = points.slice(0, -1).filter((p) => p.missing_count > 0).length;
  const change = latest && points.length > 1 ? subtractDecimalStrings(latest.value, points[0].value) : null;
  const flow = cashFlow.data;
  const error =
    settings.error ?? accounts.error ?? netWorth.error ?? holdings.error ?? cashFlow.error;

  const own = (accounts.data ?? []).filter((a) => !a.system);
  const hidden = own.filter((a) => a.excluded_from_reports).length;
  const toReview = (transfers.data ?? []).filter(needsAttention).length;

  // What each account is worth on the last day, largest first.
  const byAccount = useMemo(() => {
    const rows = groupByAccount((holdings.data ?? []).filter((h) => h.value !== null)).map(
      (group) => ({
        id: group[0].account_id,
        name: group[0].account_name,
        value: sumDecimalStrings(group.map((h) => h.value as string)),
      }),
    );
    return rows.sort((a, b) => Number(b.value) - Number(a.value));
  }, [holdings.data]);
  const largest = byAccount.reduce((max, row) => Math.max(max, Math.abs(Number(row.value))), 0);

  if (accounts.isSuccess && own.length === 0) return <Welcome />;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
        <RangePicker choice={choice} onChange={setChoice} />
      </div>

      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}

      {(toReview > 0 || hidden > 0) && (
        <div className="flex flex-wrap gap-2 text-sm">
          {toReview > 0 && (
            <Link
              to="/transfers"
              className="rounded-full border border-warning/40 bg-warning/10 px-3 py-1 text-warning hover:bg-warning/20"
            >
              {toReview === 1 ? "1 transfer needs a look" : `${toReview} transfers need a look`}
            </Link>
          )}
          {hidden > 0 && (
            <Link
              to="/accounts"
              className="rounded-full border px-3 py-1 text-muted-foreground hover:text-foreground"
            >
              {hidden === 1 ? "1 account is hidden from these numbers" : `${hidden} accounts are hidden from these numbers`}
            </Link>
          )}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardDescription>Net worth on {dayLabel(end)}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {latest && (latest.missing_count > 0 || earlierIncomplete > 0) && (
            <Notice>
              {latest.missing_count > 0 && <p>{missingMessage(latest.missing_count)}</p>}
              {earlierIncomplete > 0 && <p>{earlierIncompleteMessage(earlierIncomplete)}</p>}
            </Notice>
          )}
          {currency && latest ? (
            <>
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <p className="text-4xl font-semibold tracking-tight tabular-nums sm:text-5xl">
                  {formatMoney(latest.value, currency)}
                </p>
                {change !== null && (
                  <p
                    className={`flex items-center gap-1 text-sm tabular-nums ${
                      change.startsWith("-") ? "text-spending" : "text-income"
                    }`}
                  >
                    {change.startsWith("-") ? (
                      <ArrowDownRight className="size-4" aria-hidden />
                    ) : (
                      <ArrowUpRight className="size-4" aria-hidden />
                    )}
                    {formatMoney(change, currency)} since {dayLabel(start)}
                  </p>
                )}
              </div>
              <NetWorthChart points={points} currency={currency} />
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              {netWorth.isPending || settings.isPending ? "Loading…" : "No data yet"}
            </p>
          )}
        </CardContent>
      </Card>

      {currency && flow && (
        <>
          {flow.missing_count > 0 && <Notice>{missingFlowMessage(flow.missing_count)}</Notice>}
          <div className="grid gap-4 sm:grid-cols-3">
            <Figure label="Income" value={formatMoney(flow.income, currency)} tone="text-income" />
            <Figure
              label="Spending"
              value={formatMoney(flow.spending, currency)}
              tone="text-spending"
            />
            <Figure
              label="Left over"
              value={formatMoney(flow.net, currency)}
              note={
                Number(flow.income) > 0
                  ? `${Math.round(percent(flow.net, flow.income))}% of income`
                  : undefined
              }
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-5">
            <Card className="lg:col-span-3">
              <CardHeader>
                <CardTitle>Income and spending</CardTitle>
                <CardDescription>
                  Transfers between your own accounts are not counted
                </CardDescription>
              </CardHeader>
              <CardContent>
                <CashFlowChart flow={flow} currency={currency} />
              </CardContent>
            </Card>
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>Spending by category</CardTitle>
              </CardHeader>
              <CardContent>
                <SpendingByCategory flow={flow} currency={currency} />
              </CardContent>
            </Card>
          </div>
        </>
      )}

      {currency && flow && flow.hidden_accounts.length > 0 && (
        <HiddenAccounts flow={flow} currency={currency} />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Where your money is</CardTitle>
          </CardHeader>
          <CardContent>
            {byAccount.length === 0 || !currency ? (
              <p className="text-sm text-muted-foreground">Nothing valued on {dayLabel(end)}</p>
            ) : (
              <ul className="space-y-3">
                {byAccount.map((row) => (
                  <BarRow
                    key={row.id}
                    name={row.name}
                    amount={formatMoney(row.value, currency)}
                    share={largest > 0 ? (Math.abs(Number(row.value)) / largest) * 100 : 0}
                    color={row.value.startsWith("-") ? CHART.spending : CHART.accent}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Where it went</CardTitle>
          </CardHeader>
          <CardContent>
            {!flow || !currency || flow.payees.length === 0 ? (
              <p className="text-sm text-muted-foreground">No spending in these dates</p>
            ) : (
              <ul className="space-y-3">
                {flow.payees.map((payee) => (
                  <BarRow
                    key={payee.name}
                    name={payee.name}
                    amount={formatMoney(payee.spending, currency)}
                    share={percent(payee.spending, flow.payees[0].spending)}
                    color={CHART.spending}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Holdings</CardTitle>
          <CardDescription>What each account held on {dayLabel(end)}</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead className="text-right">Quantity</TableHead>
                <TableHead className="text-right">Value</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {groupByAccount(holdings.data ?? []).map((group) => (
                <Fragment key={group[0].account_id}>
                  {group.map((holding, index) => (
                    <TableRow key={holding.instrument_id}>
                      {index === 0 && (
                        <TableCell rowSpan={group.length} className="align-top font-medium">
                          {holding.account_name}
                        </TableCell>
                      )}
                      <TableCell>{holding.symbol}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatQuantity(holding.quantity, MAX_DECIMALS)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {holding.value !== null && currency
                          ? formatMoney(holding.value, currency)
                          : "—"}
                      </TableCell>
                      <TableCell
                        className={holding.status === "ok" ? "text-muted-foreground" : "text-warning"}
                      >
                        {STATUS_LABELS[holding.status]}
                      </TableCell>
                    </TableRow>
                  ))}
                </Fragment>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
