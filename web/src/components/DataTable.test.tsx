import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DataTable, type Column } from "@/components/DataTable";

const columns: Column<{ id: string }>[] = [{ key: "id", header: "Id", cell: (r) => r.id }];

describe("DataTable", () => {
  it("shows a skeleton while loading and the empty message without rows", () => {
    const { rerender } = render(<DataTable columns={columns} rows={undefined} rowKey={(r) => r.id} loading />);
    expect(screen.getAllByTestId("skeleton-row")).toHaveLength(3);
    rerender(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} empty="Vuoto" />);
    expect(screen.getByText("Vuoto")).toBeInTheDocument();
  });
});
