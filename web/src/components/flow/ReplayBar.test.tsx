import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReplayBar, type ReplayBarProps } from "./ReplayBar";

const NOW = Date.parse("2026-11-28T10:00:00Z"); // 11:00 in Rome

function setup(over: Partial<ReplayBarProps> = {}) {
  const props: ReplayBarProps = {
    range: null,
    shown: null,
    playing: false,
    speed: 60,
    loading: { pages: 0, events: 0, done: false, capped: false, error: null },
    now: () => NOW,
    onRange: vi.fn(),
    onPlay: vi.fn(),
    onSpeed: vi.fn(),
    onSeek: vi.fn(),
    onClose: vi.fn(),
    ...over,
  };
  render(<ReplayBar {...props} />);
  return props;
}

describe("ReplayBar", () => {
  it("loads a preset window", async () => {
    const p = setup();
    await userEvent.click(screen.getByRole("button", { name: "ultima ora" }));
    expect(p.onRange).toHaveBeenCalledWith({ from: NOW - 3_600_000, to: NOW });
    await userEvent.click(screen.getByRole("button", { name: "ieri" }));
    expect(p.onRange).toHaveBeenLastCalledWith({ from: Date.parse("2026-11-26T23:00:00Z"), to: Date.parse("2026-11-27T23:00:00Z") });
  });

  it("loads a from–to window in Rome time and refuses one longer than 24 hours", async () => {
    const p = setup();
    fireEvent.change(screen.getByLabelText("da"), { target: { value: "2026-11-28T09:00" } });
    fireEvent.change(screen.getByLabelText("a"), { target: { value: "2026-11-28T10:30" } });
    await userEvent.click(screen.getByRole("button", { name: "Mostra" }));
    expect(p.onRange).toHaveBeenCalledWith({ from: Date.parse("2026-11-28T08:00:00Z"), to: Date.parse("2026-11-28T09:30:00Z") });

    fireEvent.change(screen.getByLabelText("da"), { target: { value: "2026-11-26T09:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Mostra" }));
    expect(screen.getByRole("alert")).toHaveTextContent("al massimo 24 ore");
    expect(p.onRange).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("da"), { target: { value: "2026-11-28T11:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Mostra" }));
    expect(screen.getByRole("alert")).toHaveTextContent("«da» deve venire prima di «a»");
  });

  it("shows the time shown, plays, pauses, changes speed and jumps", async () => {
    const range = { from: NOW - 3_600_000, to: NOW };
    const p = setup({ range, shown: NOW - 1_800_000, loading: { pages: 2, events: 1500, done: true, capped: false, error: null } });
    expect(screen.getByText(/moviola · ora mostrata/)).toHaveTextContent("moviola · ora mostrata 28/11 10:30:00");
    expect(screen.getByRole("status")).toHaveTextContent("1500 passaggi nell'intervallo");
    await userEvent.click(screen.getByRole("button", { name: "▶ Riproduci" }));
    expect(p.onPlay).toHaveBeenCalledWith(true);
    for (const s of ["1×", "10×", "60×", "300×"]) expect(screen.getByRole("button", { name: `velocità ${s}` })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "velocità 60×" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "velocità 300×" }));
    expect(p.onSpeed).toHaveBeenCalledWith(300);
    fireEvent.change(screen.getByRole("slider", { name: "ora mostrata" }), { target: { value: String(NOW - 600_000) } });
    expect(p.onSeek).toHaveBeenCalledWith(NOW - 600_000);
    await userEvent.click(screen.getByRole("button", { name: "Torna al vivo" }));
    expect(p.onClose).toHaveBeenCalled();
  });

  it("says when the window is still loading", () => {
    const range = { from: NOW - 3_600_000, to: NOW };
    setup({ range, shown: range.from, loading: { pages: 1, events: 1000, done: false, capped: false, error: null } });
    expect(screen.getByRole("status")).toHaveTextContent("caricamento");
  });

  it("warns on a capped or unreadable window", () => {
    const range = { from: NOW - 3_600_000, to: NOW };
    setup({ range, shown: range.from, loading: { pages: 50, events: 50_000, done: true, capped: true, error: null } });
    expect(screen.getByRole("status")).toHaveTextContent("intervallo molto fitto");
  });

  it("says when the window can not be read", () => {
    const range = { from: NOW - 3_600_000, to: NOW };
    setup({ range, shown: range.from, loading: { pages: 0, events: 0, done: true, capped: false, error: "intervallo non leggibile, riprova" } });
    expect(screen.getByRole("alert")).toHaveTextContent("intervallo non leggibile, riprova");
  });

  it("refuses a «da» in the future", async () => {
    const p = setup();
    fireEvent.change(screen.getByLabelText("da"), { target: { value: "2026-11-28T12:00" } });
    fireEvent.change(screen.getByLabelText("a"), { target: { value: "2026-11-28T13:00" } });
    await userEvent.click(screen.getByRole("button", { name: "Mostra" }));
    expect(screen.getByRole("alert")).toHaveTextContent("«da» non può essere nel futuro");
    expect(p.onRange).not.toHaveBeenCalled();
  });

  it("reads the time shown aloud on the scrub", () => {
    const range = { from: NOW - 3_600_000, to: NOW };
    setup({ range, shown: NOW - 1_800_000, loading: { pages: 1, events: 1, done: true, capped: false, error: null } });
    expect(screen.getByRole("slider", { name: "ora mostrata" })).toHaveAttribute("aria-valuetext", "10:30:00");
  });

  it("uses the last US session given by SMTrading", async () => {
    const lastSession = { from: Date.parse("2026-11-25T14:30:00Z"), to: Date.parse("2026-11-25T18:00:00Z") }; // a half day
    const p = setup({ lastSession });
    await userEvent.click(screen.getByRole("button", { name: "ultima seduta USA" }));
    expect(p.onRange).toHaveBeenCalledWith(lastSession);
  });
});
