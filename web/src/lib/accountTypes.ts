import type { AccountOut, AccountTypeOut, ImportCatalogEntryOut } from "../api";

// Each institution's types, in catalog order.
export function byInstitution(types: AccountTypeOut[]): [string, AccountTypeOut[]][] {
  const groups = new Map<string, AccountTypeOut[]>();
  for (const type of types) {
    groups.set(type.institution, [...(groups.get(type.institution) ?? []), type]);
  }
  return [...groups];
}

// The files an upload reads with a profile, as catalog formats, the recommended one (the
// most complete) first. A file only a connection reads has no profile.
export function statementFormats(type: AccountTypeOut): ImportCatalogEntryOut[] {
  return type.files
    .flatMap((file) =>
      file.config === null
        ? []
        : [
            {
              institution: type.name,
              label: file.label,
              format: file.format,
              config: file.config,
              recommended: file.recommended,
            },
          ],
    )
    .sort((a, b) => Number(b.recommended) - Number(a.recommended));
}

// How a type's activity arrives, for its tile.
export function tileDetail(type: AccountTypeOut): string {
  if (type.api) {
    const csv = type.files.some((file) => file.format === "csv");
    return csv ? "Syncs automatically · CSV export optional" : "Syncs automatically";
  }
  if (type.files.length === 1) return `Upload its ${type.files[0].label}`;
  const recommended = type.files.find((file) => file.recommended) ?? type.files[0];
  return `Upload statements · ${recommended.label} recommended`;
}

// An account's type; none when it has no key or the catalog no longer has its key.
export function typeOf(types: AccountTypeOut[], account: AccountOut): AccountTypeOut | undefined {
  if (account.type_key === null) return undefined;
  return types.find((type) => type.key === account.type_key);
}
