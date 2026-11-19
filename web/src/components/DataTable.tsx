import { ArrowDown, ArrowUp } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useIsNarrow } from "@/lib/media";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  /** Makes the column sortable by this value (strings compare it-IT, numbers numerically). */
  sortValue?: (row: T) => string | number;
  className?: string;
  /** phone cards: the label of the value (default: `header`) */
  mobileLabel?: ReactNode;
  /** phone cards: the card's title (no label, first, full width) */
  primary?: boolean;
  /** phone cards: left out (e.g. a detail the desktop table shows next to the title) */
  hideOnMobile?: boolean;
  /** phone cards: a long value (a text) under its label, on the full width */
  mobileFull?: boolean;
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  loading?: boolean;
  empty?: string;
  skeletonRows?: number;
  "aria-label"?: string;
  /** under 640px: "cards" (default) stacks every row as a card, "table" keeps the table
   * (it scrolls sideways inside its own box, never the page) */
  mobile?: "cards" | "table";
}

type Sort = { key: string; dir: "asc" | "desc" } | null;

const collator = new Intl.Collator("it-IT", { numeric: true, sensitivity: "base" });

function compare(a: string | number, b: string | number): number {
  return typeof a === "number" && typeof b === "number" ? a - b : collator.compare(String(a), String(b));
}

interface CardsProps<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  loading: boolean;
  empty: string;
  skeletonRows: number;
  ariaLabel?: string;
  sort: Sort;
  setSort: (s: Sort) => void;
}

/** The phone layout of DataTable: one card per row, label and value side by side. */
function Cards<T>({ columns, rows, rowKey, loading, empty, skeletonRows, ariaLabel, sort, setSort }: CardsProps<T>) {
  const visible = columns.filter((c) => !c.hideOnMobile);
  const primary = visible.filter((c) => c.primary);
  const rest = visible.filter((c) => !c.primary);
  const sortable = columns.filter((c) => c.sortValue && typeof c.header === "string");
  return (
    <div className="space-y-2" aria-busy={loading || undefined}>
      {sortable.length > 0 && rows && rows.length > 1 && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <label className="flex items-center gap-2 text-muted">
            Ordina per
            <select
              className="h-9 rounded-md border border-border bg-bg px-2 text-fg"
              value={sort ? `${sort.key}:${sort.dir}` : ""}
              onChange={(e) => {
                const [key, dir] = e.target.value.split(":");
                setSort(key ? { key, dir: dir as "asc" | "desc" } : null);
              }}
            >
              <option value="">—</option>
              {sortable.flatMap((c) => [
                <option key={`${c.key}:asc`} value={`${c.key}:asc`}>
                  {`${c.header as string} ↑`}
                </option>,
                <option key={`${c.key}:desc`} value={`${c.key}:desc`}>
                  {`${c.header as string} ↓`}
                </option>,
              ])}
            </select>
          </label>
        </div>
      )}
      <ul aria-label={ariaLabel} className="space-y-2" data-testid="table-cards">
        {loading && !rows ? (
          Array.from({ length: skeletonRows }, (_, i) => (
            <li key={`skeleton-${i}`} data-testid="skeleton-row" className="rounded-md border border-border p-3">
              <div className="h-4 w-2/3 animate-pulse rounded bg-card" />
              <div className="mt-2 h-4 w-full animate-pulse rounded bg-card" />
            </li>
          ))
        ) : !rows || rows.length === 0 ? (
          <li className="rounded-md border border-border py-8 text-center text-sm text-muted">{empty}</li>
        ) : (
          rows.map((row) => (
            <li key={rowKey(row)} className="rounded-md border border-border p-3 text-sm">
              {primary.map((c) => (
                <div key={c.key} className="mb-2 min-w-0 break-words font-medium">
                  {c.cell(row)}
                </div>
              ))}
              <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1.5">
                {rest.map((c) =>
                  c.mobileFull ? (
                    <div key={c.key} className="col-span-2 min-w-0">
                      <dt className="text-muted">{c.mobileLabel ?? c.header}</dt>
                      <dd className="min-w-0 break-words">{c.cell(row)}</dd>
                    </div>
                  ) : (
                    <div key={c.key} className="contents">
                      <dt className="text-muted">{c.mobileLabel ?? c.header}</dt>
                      <dd className="min-w-0 break-words text-right">{c.cell(row)}</dd>
                    </div>
                  ),
                )}
              </dl>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

/** A table with optional client-side sorting, a loading skeleton and an empty state; on a
 * phone (under 640px) the rows are stacked cards (`mobile="table"` keeps the table). */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  empty = "Nessun dato",
  skeletonRows = 3,
  "aria-label": ariaLabel,
  mobile = "cards",
}: Props<T>) {
  const [sort, setSort] = useState<Sort>(null);
  const narrow = useIsNarrow();
  const sortCol = sort && columns.find((c) => c.key === sort.key);
  const sorted =
    rows && sortCol?.sortValue
      ? [...rows].sort((a, b) => {
          const r = compare(sortCol.sortValue!(a), sortCol.sortValue!(b));
          return sort!.dir === "asc" ? r : -r;
        })
      : rows;

  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: "asc" } : s.dir === "asc" ? { key, dir: "desc" } : null));

  if (narrow && mobile === "cards") {
    return (
      <Cards
        columns={columns}
        rows={sorted}
        rowKey={rowKey}
        loading={loading}
        empty={empty}
        skeletonRows={skeletonRows}
        ariaLabel={ariaLabel}
        sort={sort}
        setSort={setSort}
      />
    );
  }

  return (
    <Table aria-label={ariaLabel} aria-busy={loading || undefined}>
      <TableHeader>
        <TableRow>
          {columns.map((c) => {
            const active = sort?.key === c.key ? sort.dir : null;
            return (
              <TableHead
                key={c.key}
                className={cn(c.align === "right" && "text-right", c.className)}
                aria-sort={active ? (active === "asc" ? "ascending" : "descending") : undefined}
              >
                {c.sortValue ? (
                  <button
                    type="button"
                    onClick={() => toggle(c.key)}
                    className="inline-flex items-center gap-1 hover:text-fg"
                  >
                    {c.header}
                    {active === "asc" && <ArrowUp className="h-3 w-3" />}
                    {active === "desc" && <ArrowDown className="h-3 w-3" />}
                  </button>
                ) : (
                  c.header
                )}
              </TableHead>
            );
          })}
        </TableRow>
      </TableHeader>
      <TableBody>
        {loading && !rows ? (
          Array.from({ length: skeletonRows }, (_, i) => (
            <TableRow key={`skeleton-${i}`} data-testid="skeleton-row">
              {columns.map((c) => (
                <TableCell key={c.key}>
                  <div className="h-4 w-full animate-pulse rounded bg-card" />
                </TableCell>
              ))}
            </TableRow>
          ))
        ) : !sorted || sorted.length === 0 ? (
          <TableRow>
            <TableCell colSpan={columns.length} className="py-8 text-center text-muted">
              {empty}
            </TableCell>
          </TableRow>
        ) : (
          sorted.map((row) => (
            <TableRow key={rowKey(row)}>
              {columns.map((c) => (
                <TableCell key={c.key} className={cn(c.align === "right" && "text-right", c.className)}>
                  {c.cell(row)}
                </TableCell>
              ))}
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  );
}
