import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { RouteError } from "@/components/RouteError";

function Boom(): never {
  throw new Error("Cannot read properties of undefined (reading 'items')");
}

describe("RouteError", () => {
  it("replaces the raw router error with an Italian page and a reload button", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const router = createMemoryRouter([{ path: "/", element: <Boom />, errorElement: <RouteError /> }]);
      render(<RouterProvider router={router} />);
      expect(screen.getByRole("heading", { name: "Si è verificato un errore nella pagina" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Ricarica" })).toBeInTheDocument();
      expect(screen.getByText(/reading 'items'/)).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
