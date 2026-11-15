import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Logo } from "@/components/Logo";

describe("Logo", () => {
  it("is decorative by default", () => {
    render(<Logo />);
    expect(screen.getByTestId("app-logo")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("gets an accessible name when standalone", () => {
    render(<Logo label="SMTrading" />);
    expect(screen.getByRole("img", { name: "SMTrading" })).toBeInTheDocument();
  });
});
