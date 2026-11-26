import type { AccountCreate } from "../api";

// What a new account asks in plain words. The ledger's own terms (ledger type, balance
// mode) follow from the answers; income, expense and equity accounts are the ledger's to
// keep, so the UI creates only what the owner has or owes.
export type Side = "have" | "owe";
export type BalanceSource = "files" | "typed";

export const SIDES: { value: Side; label: string }[] = [
  { value: "have", label: "Money I have" },
  { value: "owe", label: "Money I owe" },
];

export const BALANCE_SOURCES: { value: BalanceSource; label: string }[] = [
  { value: "files", label: "From statement files" },
  { value: "typed", label: "I'll type the balance" },
];

export function accountBody(fields: {
  name: string;
  side: Side;
  balance: BalanceSource;
  subtype?: AccountCreate["subtype"];
  institutionName?: string;
  taxWrapper?: NonNullable<AccountCreate["tax_wrapper"]>;
}): AccountCreate {
  return {
    name: fields.name.trim(),
    ledger_type: fields.side === "owe" ? "liability" : "asset",
    subtype: fields.subtype ?? "bank",
    // A statement's rows are transactions; a typed balance is a snapshot.
    balance_mode: fields.balance === "files" ? "transactions" : "snapshots",
    institution_name: fields.institutionName?.trim() || null,
    tax_wrapper: fields.taxWrapper ?? "none",
  };
}
