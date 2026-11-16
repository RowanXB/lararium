import type { QueryClient } from "@tanstack/react-query";

// Any write to identifiers or transfer decisions rematches transfers, which can move
// balances and reopen a transfer under a new id. So nothing keeps a transfer id across
// writes: each refetches everything that shows transfers or their effects.
const AFFECTED = [
  ["transfers"],
  ["transfer-suggestions"],
  ["account-identifiers"],
  ["net-worth"],
  ["holdings"],
];

export async function refreshAfterTransferWrite(queryClient: QueryClient) {
  await Promise.all(AFFECTED.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
}
