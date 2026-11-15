import {
  useIsFetching,
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import {
  type AccountOut,
  addAccountIdentifier,
  type CandidateOut,
  dismissTransferSuggestion,
  listAccounts,
  listTransferCandidates,
  listTransfers,
  listTransferSuggestions,
  pairTransfer,
  rejectTransfer,
  type SuggestionOut,
  type TransferOut,
  unpairTransfer,
  unrejectTransfer,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { errorMessage } from "../lib/errors";
import { formatSortCode } from "../lib/format";
import { refreshAfterTransferWrite } from "../lib/refresh";

const REVIEW_REASONS: Record<NonNullable<TransferOut["review_reason"]>, string> = {
  several_candidates: "Several candidates",
  amount_differs: "Amount differs",
};

const STATUS_LABELS: Record<TransferOut["status"], string> = {
  awaiting: "awaiting",
  inferred: "inferred",
  matched: "matched",
  before_history: "before history",
  rejected: "rejected",
};

const amountFormat = new Intl.NumberFormat("en-GB", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 20,
});

function formatAmount(value: string): string {
  return amountFormat.format(value as Intl.StringNumericLiteral);
}

function describe(transfer: TransferOut): string {
  const amount = `${formatAmount(transfer.amount)} ${transfer.currency}`;
  return `${transfer.from_account_name} → ${transfer.to_account_name} · ${amount}`;
}

function describeInTransit(transfer: TransferOut): string {
  const days = transfer.days_in_transit;
  if (days === null) return describe(transfer);
  return `${describe(transfer)} · ${days} ${days === 1 ? "day" : "days"} in transit`;
}

function needsAttention(transfer: TransferOut): boolean {
  return transfer.status !== "rejected" && (transfer.review_reason !== null || transfer.overdue);
}


const TRANSFER_WRITE = ["transfer-write"];

function useWrite<T>(write: (arg: T) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: TRANSFER_WRITE,
    mutationFn: write,
    onSettled: () => refreshAfterTransferWrite(queryClient),
  });
}

// A write rematches transfers, which can reopen any of them under a new id, so while any
// write or the refetch after it is in flight, every action on the page waits: none acts
// on an id that may be gone.
function useBusy(): boolean {
  const writing = useIsMutating({ mutationKey: TRANSFER_WRITE });
  const fetching =
    useIsFetching({ queryKey: ["transfers"] }) +
    useIsFetching({ queryKey: ["transfer-suggestions"] });
  return writing + fetching > 0;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card role="region" aria-label={title}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

function useCandidates(transferId: string, enabled = true) {
  return useQuery({
    queryKey: ["transfers", transferId, "candidates"],
    queryFn: async () =>
      (await listTransferCandidates({ path: { transfer_id: transferId }, throwOnError: true }))
        .data,
    enabled,
  });
}

function usePair(transferId: string) {
  return useWrite(async (transactionId: string) =>
    pairTransfer({
      path: { transfer_id: transferId },
      body: { transaction_id: transactionId },
      throwOnError: true,
    }),
  );
}

function CandidateList({
  transfer,
  candidates,
  busy,
  onPair,
}: {
  transfer: TransferOut;
  candidates: CandidateOut[];
  busy: boolean;
  onPair: (transactionId: string) => void;
}) {
  if (candidates.length === 0) return <Empty>No candidates</Empty>;
  return (
    <ul className="space-y-2">
      {candidates.map((candidate) => (
        <li
          key={candidate.transaction_id}
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2 text-sm"
        >
          <span className="min-w-0 break-words">
            {[
              candidate.account_name,
              new Date(candidate.occurred_at).toLocaleDateString("en-GB"),
              `${formatAmount(candidate.quantity.replace(/^-/, ""))} ${transfer.currency}`,
              candidate.description,
            ].join(" · ")}
          </span>
          {/* The API pairs a candidate of the same amount, or one the owner unpaired. */}
          {candidate.reason === "amount_differs" ? (
            <span className="text-muted-foreground">Different amount</span>
          ) : (
            <span className="flex items-center gap-2">
              {candidate.reason === "unpaired" && (
                <span className="text-muted-foreground">Unpaired</span>
              )}
              <Button
                size="sm"
                disabled={busy}
                onClick={() => onPair(candidate.transaction_id)}
              >
                Pair
              </Button>
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function AttentionItem({ transfer }: { transfer: TransferOut }) {
  const candidates = useCandidates(transfer.id);
  const pair = usePair(transfer.id);
  const reject = useWrite(async () =>
    rejectTransfer({ path: { transfer_id: transfer.id }, throwOnError: true }),
  );
  const error = pair.error ?? reject.error ?? candidates.error;
  const busy = useBusy();

  return (
    <li className="space-y-2 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium">{describe(transfer)}</p>
          <p className="text-sm text-amber-600">
            {[
              transfer.review_reason && REVIEW_REASONS[transfer.review_reason],
              transfer.overdue && "Overdue",
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => {
            pair.reset();
            reject.mutate();
          }}
        >
          Reject
        </Button>
      </div>
      {candidates.data && (
        <CandidateList
          transfer={transfer}
          candidates={candidates.data}
          busy={busy}
          onPair={(transactionId) => {
            reject.reset();
            pair.mutate(transactionId);
          }}
        />
      )}
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
    </li>
  );
}

// A transfer missing one side that needs no attention can still have candidates: the
// records the owner unpaired from it, which pairing links again. Fetched when opened.
function OpenCandidates({ transfer }: { transfer: TransferOut }) {
  const [open, setOpen] = useState(false);
  const candidates = useCandidates(transfer.id, open);
  const pair = usePair(transfer.id);
  const error = pair.error ?? candidates.error;
  const busy = useBusy();

  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-sm text-muted-foreground">Candidates</summary>
      <div className="mt-2 space-y-2">
        {candidates.data && (
          <CandidateList
            transfer={transfer}
            candidates={candidates.data}
            busy={busy}
            onPair={(transactionId) => pair.mutate(transactionId)}
          />
        )}
        {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
      </div>
    </details>
  );
}

function RecentItem({ transfer }: { transfer: TransferOut }) {
  const path = { transfer_id: transfer.id };
  const action = useWrite(async () => {
    if (transfer.status === "matched") return unpairTransfer({ path, throwOnError: true });
    if (transfer.status === "rejected") return unrejectTransfer({ path, throwOnError: true });
    return rejectTransfer({ path, throwOnError: true });
  });
  const label =
    transfer.status === "matched" ? "Unpair" : transfer.status === "rejected" ? "Undo" : "Reject";
  const busy = useBusy();

  return (
    <li className="space-y-1 py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-sm break-words">
          {`${describe(transfer)} · ${STATUS_LABELS[transfer.status]}`}
        </p>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => action.mutate()}>
          {label}
        </Button>
      </div>
      {action.isError && (
        <p className="text-sm text-destructive">{errorMessage(action.error)}</p>
      )}
      {(transfer.status === "inferred" || transfer.status === "before_history") && (
        <OpenCandidates transfer={transfer} />
      )}
    </li>
  );
}

function SuggestionItem({
  suggestion,
  accounts,
}: {
  suggestion: SuggestionOut;
  accounts: AccountOut[];
}) {
  const [accountId, setAccountId] = useState<string>();
  const register = useWrite(async (account: string) =>
    addAccountIdentifier({
      path: { account_id: account },
      body: {
        kind: "uk_bank_account",
        value: `${suggestion.sort_code}:${suggestion.account_number}`,
      },
      throwOnError: true,
    }),
  );
  const dismiss = useWrite(async () =>
    dismissTransferSuggestion({
      path: { identifier_id: suggestion.identifier_id },
      throwOnError: true,
    }),
  );
  const error = register.error ?? dismiss.error;
  const busy = useBusy();

  return (
    <li className="space-y-2 py-4 first:pt-0 last:pb-0">
      <div>
        <p className="font-medium">
          {`${formatSortCode(suggestion.sort_code)} · ${suggestion.account_number}`}
        </p>
        <p className="text-sm text-muted-foreground">
          {[
            suggestion.names.join(", "),
            `${suggestion.outgoing} out, ${suggestion.incoming} in`,
            `latest ${new Date(suggestion.latest).toLocaleDateString("en-GB")}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <LabelledSelect
          label="Account"
          value={accountId}
          options={accounts.map((account) => ({ value: account.id, label: account.name }))}
          onChange={setAccountId}
          placeholder="Pick an account"
        />
        <Button
          disabled={busy || !accountId}
          onClick={() => {
            if (!accountId) return;
            dismiss.reset();
            register.mutate(accountId);
          }}
        >
          Register
        </Button>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => {
            register.reset();
            dismiss.mutate();
          }}
        >
          Dismiss
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
    </li>
  );
}

export function TransfersPage() {
  const transfers = useQuery({
    queryKey: ["transfers"],
    queryFn: async () => (await listTransfers({ throwOnError: true })).data,
  });
  const suggestions = useQuery({
    queryKey: ["transfer-suggestions"],
    queryFn: async () => (await listTransferSuggestions({ throwOnError: true })).data,
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });
  const error = transfers.error ?? suggestions.error ?? accounts.error;

  const all = transfers.data ?? [];
  const attention = all.filter(needsAttention);
  const inTransit = all.filter((t) => t.status === "awaiting" && !needsAttention(t));
  const recent = all.filter((t) => t.status !== "awaiting" && !needsAttention(t));
  // Only an asset or liability account can be the owner's bank account; In transit is
  // the ledger's own, which can't have identifiers.
  const ownAccounts = (accounts.data ?? []).filter(
    (a) =>
      (a.ledger_type === "asset" || a.ledger_type === "liability") &&
      a.subtype !== "in_transit" &&
      a.status === "open",
  );

  return (
    <div className="space-y-6">
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
      <Section title="Needs attention">
        {transfers.isSuccess && attention.length === 0 && <Empty>Nothing needs attention</Empty>}
        <ul className="divide-y">
          {attention.map((transfer) => (
            <AttentionItem key={transfer.id} transfer={transfer} />
          ))}
        </ul>
      </Section>
      <Section title="In transit">
        {transfers.isSuccess && inTransit.length === 0 && <Empty>Nothing in transit</Empty>}
        <ul className="divide-y">
          {inTransit.map((transfer) => (
            <li key={transfer.id} className="space-y-1 py-3 text-sm first:pt-0 last:pb-0">
              <p>{describeInTransit(transfer)}</p>
              <OpenCandidates transfer={transfer} />
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Suggestions">
        {suggestions.data?.length === 0 && <Empty>No suggestions</Empty>}
        <ul className="divide-y">
          {suggestions.data?.map((suggestion) => (
            <SuggestionItem
              key={suggestion.identifier_id}
              suggestion={suggestion}
              accounts={ownAccounts}
            />
          ))}
        </ul>
      </Section>
      <Section title="Recent">
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            {`Recent (${recent.length})`}
          </summary>
          <ul className="mt-3 divide-y">
            {recent.map((transfer) => (
              <RecentItem key={transfer.id} transfer={transfer} />
            ))}
          </ul>
        </details>
      </Section>
    </div>
  );
}
