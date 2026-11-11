import { useQuery } from "@tanstack/react-query";
import { Fragment } from "react";

import { getFinanceSettings, getHoldings, getNetWorth, type HoldingOut } from "../api";
import { NetWorthChart } from "../components/NetWorthChart";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { errorMessage } from "../lib/errors";
import { formatMoney, formatQuantity } from "../lib/format";

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

function groupByAccount(holdings: HoldingOut[]): HoldingOut[][] {
  const groups = new Map<string, HoldingOut[]>();
  for (const holding of holdings) {
    const group = groups.get(holding.account_id);
    if (group) group.push(holding);
    else groups.set(holding.account_id, [holding]);
  }
  return [...groups.values()];
}

export function NetWorthPage() {
  const settings = useQuery({
    queryKey: ["finance-settings"],
    queryFn: async () => (await getFinanceSettings({ throwOnError: true })).data,
  });
  const netWorth = useQuery({
    queryKey: ["net-worth"],
    queryFn: async () => (await getNetWorth({ throwOnError: true })).data,
  });
  const holdings = useQuery({
    queryKey: ["holdings"],
    queryFn: async () => (await getHoldings({ throwOnError: true })).data,
  });

  const currency = settings.data?.reporting_currency;
  const points = netWorth.data ?? [];
  const latest = points.at(-1);
  const error = settings.error ?? netWorth.error ?? holdings.error;

  return (
    <div className="space-y-6">
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}

      <Card>
        <CardHeader>
          <CardTitle>Net worth</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {latest && latest.missing_count > 0 && (
            <p
              role="status"
              className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
            >
              {missingMessage(latest.missing_count)}
            </p>
          )}
          {currency && latest ? (
            <>
              <p className="text-3xl font-semibold tabular-nums">
                {formatMoney(latest.value, currency)}
              </p>
              <NetWorthChart points={points} currency={currency} />
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              {netWorth.isPending || settings.isPending ? "Loading…" : "No data yet"}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Holdings</CardTitle>
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
                      <TableCell>{STATUS_LABELS[holding.status]}</TableCell>
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
