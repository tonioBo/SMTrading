import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { killSwitchKey } from "@/api/hooks/killSwitch";
import { KillSwitchButton } from "@/components/KillSwitchButton";
import { createQueryClient } from "@/queryClient";

let posts: unknown[] = [];
const server = setupServer(
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.post("*/api/kill-switch", async ({ request }) => {
    const body = (await request.json()) as { on: boolean };
    posts.push(body);
    return HttpResponse.json({ on: body.on, canceled: 0 });
  }),
  http.get("*/api/dashboard", () => HttpResponse.json({})),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  posts = [];
});
afterAll(() => server.close());

describe("KillSwitchButton", () => {
  it("keeps the action chosen when the dialog opened, even if the state flips meanwhile", async () => {
    const user = userEvent.setup();
    const qc = createQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <KillSwitchButton />
      </QueryClientProvider>,
    );
    await user.click(await screen.findByRole("button", { name: "Attiva kill-switch" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Attivare il kill-switch?")).toBeInTheDocument();
    act(() => qc.setQueryData(killSwitchKey, { on: true })); // e.g. turned on elsewhere
    expect(within(dialog).getByText("Attivare il kill-switch?")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Attiva" }));
    await waitFor(() => expect(posts).toEqual([{ on: true, cancel_open: true }]));
  });
});
