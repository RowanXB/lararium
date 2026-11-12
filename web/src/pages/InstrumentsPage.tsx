import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import {
  createInstrument,
  type InstrumentCreate,
  type InstrumentOut,
  listInstruments,
  updateInstrument,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Checkbox } from "../components/ui/checkbox";
import { Label } from "../components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { errorMessage } from "../lib/errors";

const KINDS: { value: InstrumentCreate["kind"]; label: string }[] = [
  { value: "fiat", label: "Fiat" },
  { value: "equity", label: "Equity" },
  { value: "etf", label: "ETF" },
  { value: "fund", label: "Fund" },
  { value: "bond", label: "Bond" },
  { value: "crypto", label: "Crypto" },
  { value: "option", label: "Option" },
  { value: "cfd", label: "CFD" },
  { value: "other", label: "Other" },
];

const ALL_INSTRUMENTS = ["instruments", { includeHidden: true }] as const;

function HideCheckbox({ instrument }: { instrument: InstrumentOut }) {
  const queryClient = useQueryClient();
  const id = useId();
  const mutation = useMutation({
    mutationFn: async (hidden: boolean) =>
      (
        await updateInstrument({
          path: { instrument_id: instrument.id },
          body: { hidden },
          throwOnError: true,
        })
      ).data,
    // Every instrument list (including the dropdowns on the accounts page) depends on this.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["instruments"] }),
  });

  return (
    <div className="flex items-center gap-2">
      <Checkbox
        id={id}
        checked={instrument.hidden}
        disabled={mutation.isPending}
        onCheckedChange={(checked) => mutation.mutate(checked === true)}
      />
      <Label htmlFor={id}>Hide</Label>
      {mutation.isError && (
        <span className="text-sm text-destructive">{errorMessage(mutation.error)}</span>
      )}
    </div>
  );
}

function NewInstrumentForm() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<InstrumentCreate["kind"]>("equity");
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [decimals, setDecimals] = useState("2");
  const [quoteCurrency, setQuoteCurrency] = useState("");
  const [coingeckoId, setCoingeckoId] = useState("");

  const mutation = useMutation({
    mutationFn: async (body: InstrumentCreate) =>
      (await createInstrument({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setSymbol("");
      setName("");
      setQuoteCurrency("");
      setCoingeckoId("");
      await queryClient.invalidateQueries({ queryKey: ["instruments"] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmedSymbol = symbol.trim();
    const coingecko = coingeckoId.trim();
    const identifiers: NonNullable<InstrumentCreate["identifiers"]> = [];
    // FX rates and the reporting currency find currencies by their ISO 4217 code.
    if (kind === "fiat") identifiers.push({ scheme: "iso4217", value: trimmedSymbol });
    if (coingecko) identifiers.push({ scheme: "coingecko", value: coingecko });
    mutation.mutate({
      kind,
      symbol: trimmedSymbol,
      name: name.trim(),
      decimals: Number.parseInt(decimals, 10),
      quote_currency: quoteCurrency.trim() || null,
      identifiers,
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New instrument</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="New instrument" onSubmit={submit} className="grid gap-4 sm:grid-cols-3">
          <LabelledSelect label="Kind" value={kind} options={KINDS} onChange={setKind} />
          <TextField label="Symbol" value={symbol} onChange={setSymbol} required />
          <TextField label="Name" value={name} onChange={setName} required />
          <TextField label="Decimals" type="number" value={decimals} onChange={setDecimals} required />
          <TextField label="Quote currency" value={quoteCurrency} onChange={setQuoteCurrency} />
          <TextField label="CoinGecko id (optional)" value={coingeckoId} onChange={setCoingeckoId} />
          <div className="flex items-center gap-3 sm:col-span-3">
            <Button type="submit" disabled={mutation.isPending}>
              Create instrument
            </Button>
            {mutation.isError && (
              <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

export function InstrumentsPage() {
  const instruments = useQuery({
    queryKey: ALL_INSTRUMENTS,
    queryFn: async () =>
      (await listInstruments({ query: { include_hidden: true }, throwOnError: true })).data,
  });

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Instruments</CardTitle>
        </CardHeader>
        <CardContent>
          {instruments.error && (
            <p className="text-sm text-destructive">{errorMessage(instruments.error)}</p>
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Symbol</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead className="text-right">Decimals</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {instruments.data?.map((instrument) => (
                <TableRow key={instrument.id}>
                  <TableCell className="font-medium">{instrument.symbol}</TableCell>
                  <TableCell>{instrument.name}</TableCell>
                  <TableCell>
                    {KINDS.find((k) => k.value === instrument.kind)?.label ?? instrument.kind}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{instrument.decimals}</TableCell>
                  <TableCell>
                    <HideCheckbox instrument={instrument} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <NewInstrumentForm />
    </div>
  );
}
