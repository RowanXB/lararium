import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import {
  type FxRateCreate,
  type InstrumentOut,
  listInstruments,
  type PriceCreate,
  recordFxRate,
  recordPrice,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { todayForInput } from "../lib/dates";
import { errorMessage } from "../lib/errors";
import { isDecimalString } from "../lib/format";

const INVALID_AMOUNT = "Enter a positive number, e.g. 1234.56";

// Prices and rates feed every valuation, so refresh what the net worth page shows.
async function invalidateValuations(queryClient: ReturnType<typeof useQueryClient>) {
  await queryClient.invalidateQueries({ queryKey: ["holdings"] });
  await queryClient.invalidateQueries({ queryKey: ["net-worth"] });
}

function FormStatus({
  invalid,
  mutation,
}: {
  invalid: boolean;
  mutation: { isError: boolean; error: unknown; isSuccess: boolean };
}) {
  if (invalid) return <p className="text-sm text-destructive">{INVALID_AMOUNT}</p>;
  if (mutation.isError) {
    return <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>;
  }
  if (mutation.isSuccess) return <p className="text-sm text-muted-foreground">Saved</p>;
  return null;
}

function PriceForm({ instruments }: { instruments: InstrumentOut[] }) {
  const queryClient = useQueryClient();
  // Currencies are always worth 1 of themselves; only other instruments need prices.
  const priced = instruments.filter((instrument) => instrument.kind !== "fiat");
  const [instrumentId, setInstrumentId] = useState<string>();
  const [quoteCurrency, setQuoteCurrency] = useState("");
  const [price, setPrice] = useState("");
  const [asOfDate, setAsOfDate] = useState(todayForInput);
  const [invalid, setInvalid] = useState(false);
  const selectedInstrumentId = instrumentId ?? priced[0]?.id;

  const mutation = useMutation({
    mutationFn: async (body: PriceCreate) => (await recordPrice({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setPrice("");
      await invalidateValuations(queryClient);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const entered = price.trim();
    if (!selectedInstrumentId || !isDecimalString(entered, { unsigned: true })) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    mutation.mutate({
      instrument_id: selectedInstrumentId,
      quote_currency: quoteCurrency.trim(),
      price: entered,
      as_of_date: asOfDate,
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Record price</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Record price" onSubmit={submit} className="grid gap-4 sm:grid-cols-4">
          <LabelledSelect
            label="Instrument"
            value={selectedInstrumentId}
            options={priced.map((instrument) => ({
              value: instrument.id,
              label: instrument.symbol,
            }))}
            onChange={setInstrumentId}
            placeholder="Instrument"
          />
          <TextField
            label="Quote currency"
            value={quoteCurrency}
            onChange={setQuoteCurrency}
            required
          />
          <TextField label="Price" value={price} onChange={setPrice} required />
          <TextField label="Date" type="date" value={asOfDate} onChange={setAsOfDate} required />
          <div className="flex items-center gap-3 sm:col-span-4">
            <Button type="submit" disabled={mutation.isPending}>
              Record price
            </Button>
            <FormStatus invalid={invalid} mutation={mutation} />
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function FxRateForm() {
  const queryClient = useQueryClient();
  const [base, setBase] = useState("");
  const [quote, setQuote] = useState("");
  const [rate, setRate] = useState("");
  const [asOfDate, setAsOfDate] = useState(todayForInput);
  const [invalid, setInvalid] = useState(false);

  const mutation = useMutation({
    mutationFn: async (body: FxRateCreate) =>
      (await recordFxRate({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setRate("");
      await invalidateValuations(queryClient);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const entered = rate.trim();
    if (!isDecimalString(entered, { unsigned: true })) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    mutation.mutate({ base: base.trim(), quote: quote.trim(), rate: entered, as_of_date: asOfDate });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Record FX rate</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Record FX rate" onSubmit={submit} className="grid gap-4 sm:grid-cols-4">
          <TextField label="Base currency" value={base} onChange={setBase} required />
          <TextField label="Quote currency" value={quote} onChange={setQuote} required />
          <TextField label="Rate" value={rate} onChange={setRate} required />
          <TextField label="Date" type="date" value={asOfDate} onChange={setAsOfDate} required />
          <div className="flex items-center gap-3 sm:col-span-4">
            <Button type="submit" disabled={mutation.isPending}>
              Record FX rate
            </Button>
            <FormStatus invalid={invalid} mutation={mutation} />
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

export function PricesPage() {
  const instruments = useQuery({
    queryKey: ["instruments", { includeHidden: false }],
    queryFn: async () => (await listInstruments({ throwOnError: true })).data,
  });

  return (
    <div className="space-y-6">
      {instruments.error && (
        <p className="text-sm text-destructive">{errorMessage(instruments.error)}</p>
      )}
      <PriceForm instruments={instruments.data ?? []} />
      <FxRateForm />
    </div>
  );
}
