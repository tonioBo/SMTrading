import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { DataTable, type Column } from "@/components/DataTable";
import { resetViewport, setViewportWidth } from "@/test/viewport";

const columns: Column<{ id: string }>[] = [{ key: "id", header: "Id", cell: (r) => r.id }];

interface Row {
  id: string;
  name: string;
  qty: number;
  note: string;
}
const rows: Row[] = [
  { id: "1", name: "AAPL", qty: 3, note: "a" },
  { id: "2", name: "MSFT", qty: 10, note: "b" },
];
const richColumns: Column<Row>[] = [
  { key: "name", header: "Simbolo", primary: true, sortValue: (r) => r.name, cell: (r) => <strong>{r.name}</strong> },
  { key: "qty", header: "Quantità", align: "right", sortValue: (r) => r.qty, cell: (r) => r.qty },
  { key: "note", header: "Nota", hideOnMobile: true, cell: (r) => r.note },
];

afterEach(() => resetViewport());

describe("DataTable", () => {
  it("shows a skeleton while loading and the empty message without rows", () => {
    const { rerender } = render(<DataTable columns={columns} rows={undefined} rowKey={(r) => r.id} loading />);
    expect(screen.getAllByTestId("skeleton-row")).toHaveLength(3);
    rerender(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} empty="Vuoto" />);
    expect(screen.getByText("Vuoto")).toBeInTheDocument();
  });

  it("is a table on a desktop width", () => {
    setViewportWidth(1280);
    render(<DataTable aria-label="Posizioni" columns={richColumns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByRole("table", { name: "Posizioni" })).toBeInTheDocument();
    expect(screen.queryByTestId("table-cards")).not.toBeInTheDocument();
  });

  it("stacks the rows as cards under 640px, with labels, the title first and hidden columns left out", async () => {
    setViewportWidth(390);
    const user = userEvent.setup();
    render(<DataTable aria-label="Posizioni" columns={richColumns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Posizioni" });
    const cards = within(list).getAllByRole("listitem");
    expect(cards).toHaveLength(2);
    expect(cards[0].firstElementChild).toHaveTextContent("AAPL"); // the primary column as the title
    expect(within(cards[0]).getByText("Quantità")).toBeInTheDocument(); // label next to the value
    expect(within(cards[0]).queryByText("Nota")).not.toBeInTheDocument();
    expect(within(cards[0]).queryByText("a")).not.toBeInTheDocument();
    // the sortable columns become a «Ordina per» select
    await user.selectOptions(screen.getByLabelText("Ordina per"), "qty:desc");
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("MSFT");
  });

  it("switches live when the width changes and keeps the table on request", () => {
    setViewportWidth(1024);
    const { rerender } = render(<DataTable aria-label="T" columns={richColumns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    setViewportWidth(360);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    rerender(<DataTable aria-label="T" mobile="table" columns={richColumns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("shows the skeleton and the empty message as cards too", () => {
    setViewportWidth(360);
    const { rerender } = render(<DataTable columns={columns} rows={undefined} rowKey={(r) => r.id} loading />);
    expect(screen.getAllByTestId("skeleton-row")).toHaveLength(3);
    rerender(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} empty="Vuoto" />);
    expect(screen.getByText("Vuoto")).toBeInTheDocument();
  });
});
