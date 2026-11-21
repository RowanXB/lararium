import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import {
  commitImport,
  discardImport,
  getImportBatch,
  type ImportRowOut,
  type ImportStatementOut,
  listAccounts,
  listInstruments,
  rollbackImport,
} from "../api";
import { Button } from "../components/ui/button";
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
import { formatMoney, isZeroDecimal } from "../lib/format";
import { BATCH_STATUS_LABELS, countsSummary, isoCode, ROW_STATUS_LABELS } from "../lib/imports";
import { cn } from "../lib/utils";

const OPENING_LABEL = "Opening balance adjustment";
const GAP_LABEL = "Balance gap: a statement period may be missing";

const BADGE_CLASSES: Record<ImportRowOut["status"], string> = {
  new: "bg-primary/10 text-primary",
  committed: "bg-primary/10 text-primary",
  duplicate: "bg-muted text-muted-foreground",
  skipped: "bg-muted text-muted-foreground",
  error: "bg-destructive/10 text-destructive",
  rolled_back: "bg-muted text-muted-foreground",
};

// The typed fields a file gave the row, beyond its date, description and amount.
function rowDetails(row: ImportRowOut, code: string | null): string[] {
  const counterparty = [
    row.counterparty_name,
    row.counterparty_sort_code,
    row.counterparty_account_number,
    row.counterparty_account_ending && `account ending ${row.counterparty_account_ending}`,
    row.counterparty_bank,
  ]
    .filter(Boolean)
    .join(", ");
  const details: [string, string | null][] = [
    // Charged on top of the amount: the balance moves by amount minus fee (Decision 11).
    // A zero fee is no fee: every row has one, as the file states none (Decision 11).
    ["Fee", isZeroDecimal(row.fee) ? null : money(row.fee, code)],
    ["Value date", row.value_date],
    ["Payee", row.payee],
    ["Memo", row.memo],
    ["Check number", row.check_number],
    ["Counterparty", counterparty || null],
    ["Payment kind", row.payment_kind],
    ["Reference", row.payment_reference],
    ["Bank reference", row.bank_reference],
    ["Purpose code", row.purpose_code],
    ["Remittance", row.remittance_info],
    // Only a time the file gave: a date-only row's occurred_at is its day's midnight.
    ["Time", row.time_unit === "minute" || row.time_unit === "second" ? row.occurred_at : null],
    ["Started", row.started_at],
    ["Card", row.card_number],
    [
      "Converted from",
      row.original_amount === null
        ? row.original_currency
        : money(row.original_amount, row.original_currency),
    ],
    ["Exchange rate", row.exchange_rate],
    // Units of the original currency per one of the row's, as the bank prints them.
    ["Bank rate", row.fx_rate],
    ["Reference rate", row.fx_reference_rate],
    ["Type", row.bank_type],
  ];
  return details.flatMap(([label, value]) => (value ? [`${label}: ${value}`] : []));
}

function money(value: string, currency: string | null | undefined): string {
  return currency ? formatMoney(value, currency) : value;
}

function StatementSummary({ statement }: { statement: ImportStatementOut }) {
  const items: [string, string][] = [];
  if (statement.period_from || statement.period_to) {
    items.push(["Period", `${statement.period_from ?? "?"} to ${statement.period_to ?? "?"}`]);
  }
  const amounts: [string, string | null][] = [
    ["Opening balance", statement.opening_balance],
    ["Closing balance", statement.closing_balance],
    ["Money in", statement.money_in],
    ["Money out", statement.money_out],
    ["Interest", statement.interest],
  ];
  for (const [label, value] of amounts) {
    if (value !== null) items.push([label, money(value, statement.currency)]);
  }
  if (statement.payments_in !== null) items.push(["Payments in", String(statement.payments_in)]);
  if (statement.payments_out !== null) {
    items.push(["Payments out", String(statement.payments_out)]);
  }
  // Tembo prints an identifier that isn't a number instead.
  const account = statement.account_number ?? statement.account_reference;
  if (account) {
    items.push(["Account", `Account ending ${account.slice(-4)}`]);
  }
  if (items.length === 0) return null;
  return (
    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-muted-foreground">{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// Opening adjustments come first, then the file's rows in order.
function ordered(rows: ImportRowOut[]): ImportRowOut[] {
  const isAdjustment = (row: ImportRowOut) => (row.kind === "opening_adjustment" ? 0 : 1);
  return [...rows].sort((a, b) => isAdjustment(a) - isAdjustment(b) || a.position - b.position);
}

export function ImportBatchPage({ batchId }: { batchId: string }) {
  const queryClient = useQueryClient();
  // Discard and roll back can't be undone, so each asks for a second click.
  const [confirming, setConfirming] = useState<"discard" | "rollback" | null>(null);
  const batch = useQuery({
    queryKey: ["import-batch", batchId],
    queryFn: async () =>
      (await getImportBatch({ path: { batch_id: batchId }, throwOnError: true })).data,
  });
  // A row's currency may be one hidden from the pickers; it still needs its code.
  const instruments = useQuery({
    queryKey: ["instruments", { includeHidden: true }],
    queryFn: async () =>
      (await listInstruments({ query: { include_hidden: true }, throwOnError: true })).data,
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });

  const mutation = useMutation({
    mutationFn: async (action: "commit" | "discard" | "rollback") => {
      const options = { path: { batch_id: batchId }, throwOnError: true } as const;
      if (action === "commit") return (await commitImport(options)).data;
      if (action === "discard") return (await discardImport(options)).data;
      return (await rollbackImport(options)).data;
    },
    onSettled: async () => {
      setConfirming(null);
      await queryClient.invalidateQueries({ queryKey: ["imports"] });
      await queryClient.invalidateQueries({ queryKey: ["import-batch", batchId] });
      await queryClient.invalidateQueries({ queryKey: ["holdings"] });
      await queryClient.invalidateQueries({ queryKey: ["net-worth"] });
    },
  });

  const error = batch.error ?? instruments.error ?? accounts.error;
  const detail = batch.data;
  if (!detail) {
    return error ? <p className="text-sm text-destructive">{errorMessage(error)}</p> : null;
  }

  const codeOf = (instrumentId: string | null) => {
    const instrument = instruments.data?.find((i) => i.id === instrumentId);
    return instrument ? isoCode(instrument) : null;
  };
  const errors = detail.counts.error ?? 0;
  const transactions = detail.rows.filter(
    (row) => row.status === "committed" && row.transaction_id !== null,
  ).length;
  const accountName = accounts.data?.find((a) => a.id === detail.account_id)?.name;

  return (
    <div className="space-y-6">
      <Link to="/imports" className="text-sm text-muted-foreground hover:text-foreground">
        All imports
      </Link>
      <Card>
        <CardHeader>
          <CardTitle>{detail.filename}</CardTitle>
          <p className="text-sm text-muted-foreground">
            {[accountName, BATCH_STATUS_LABELS[detail.status], countsSummary(detail.counts)]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
          {detail.warnings.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {detail.warnings.map((warning, index) => (
                // Warnings have no id, and two can read the same.
                <li key={index}>{warning}</li>
              ))}
            </ul>
          )}
          {detail.statement && <StatementSummary statement={detail.statement} />}
          {detail.status === "staged" && (
            <div className="flex flex-wrap items-center gap-3">
              <Button
                disabled={errors > 0 || mutation.isPending}
                onClick={() => mutation.mutate("commit")}
              >
                Commit
              </Button>
              {confirming === "discard" ? (
                <>
                  <p className="text-sm">Discarding can't be undone</p>
                  <Button
                    variant="destructive"
                    disabled={mutation.isPending}
                    onClick={() => mutation.mutate("discard")}
                  >
                    Confirm discard
                  </Button>
                  <Button variant="outline" onClick={() => setConfirming(null)}>
                    Cancel
                  </Button>
                </>
              ) : (
                <Button
                  variant="outline"
                  disabled={mutation.isPending}
                  onClick={() => setConfirming("discard")}
                >
                  Discard
                </Button>
              )}
              {errors > 0 && (
                <p className="text-sm text-destructive">
                  Fix the errors in the file or profile and upload again
                </p>
              )}
            </div>
          )}
          {detail.status === "committed" && (
            <div className="flex flex-wrap items-center gap-3">
              {confirming === "rollback" ? (
                <>
                  <p className="text-sm">
                    This voids {transactions} {transactions === 1 ? "transaction" : "transactions"}
                  </p>
                  <Button
                    variant="destructive"
                    disabled={mutation.isPending}
                    onClick={() => mutation.mutate("rollback")}
                  >
                    Confirm roll back
                  </Button>
                  <Button variant="outline" onClick={() => setConfirming(null)}>
                    Cancel
                  </Button>
                </>
              ) : (
                <Button variant="outline" onClick={() => setConfirming("rollback")}>
                  Roll back
                </Button>
              )}
            </div>
          )}
          {mutation.isError && (
            <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-right">#</TableHead>
                <TableHead>Date</TableHead>
                <TableHead>Description</TableHead>
                <TableHead className="text-right">Amount</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ordered(detail.rows).map((row) => {
                const adjustment = row.kind === "opening_adjustment";
                const code = codeOf(row.instrument_id);
                const details = rowDetails(row, code);
                return (
                  <TableRow key={row.id}>
                    <TableCell className="text-right tabular-nums">
                      {adjustment ? "" : row.position}
                    </TableCell>
                    <TableCell className="tabular-nums">{row.booking_date}</TableCell>
                    <TableCell className="whitespace-normal">
                      <p className={cn(adjustment && "font-medium")}>
                        {adjustment ? (row.is_gap ? GAP_LABEL : OPENING_LABEL) : row.description}
                      </p>
                      {details.length > 0 && (
                        <p className="text-xs text-muted-foreground">{details.join(" · ")}</p>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.quantity === null ? "" : money(row.quantity, code)}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <span
                        className={cn(
                          "rounded-md px-1.5 py-0.5 text-xs font-medium",
                          BADGE_CLASSES[row.status],
                        )}
                      >
                        {ROW_STATUS_LABELS[row.status]}
                      </span>
                      {row.status === "error" && row.error && (
                        <p className="mt-1 text-xs text-destructive">{row.error}</p>
                      )}
                      {row.status !== "error" && row.note && (
                        <p className="mt-1 text-xs text-muted-foreground">{row.note}</p>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
