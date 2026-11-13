import { ArrowDown, ArrowUp } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  /** Makes the column sortable by this value (strings compare it-IT, numbers numerically). */
  sortValue?: (row: T) => string | number;
  className?: string;
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  loading?: boolean;
  empty?: string;
  skeletonRows?: number;
  "aria-label"?: string;
}

type Sort = { key: string; dir: "asc" | "desc" } | null;

const collator = new Intl.Collator("it-IT", { numeric: true, sensitivity: "base" });

function compare(a: string | number, b: string | number): number {
  return typeof a === "number" && typeof b === "number" ? a - b : collator.compare(String(a), String(b));
}

/** A table with optional client-side sorting, a loading skeleton and an empty state. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  empty = "Nessun dato",
  skeletonRows = 3,
  "aria-label": ariaLabel,
}: Props<T>) {
  const [sort, setSort] = useState<Sort>(null);
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
