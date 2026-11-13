import { expect, test } from "@playwright/test";
import { login } from "./helpers";

test("login e logout", async ({ page }) => {
  await page.goto("/portafogli");
  await expect(page).toHaveURL(/\/login/);
  await login(page);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await page.getByRole("button", { name: "Esci" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/portafogli");
  await expect(page).toHaveURL(/\/login/);
});

test("credenziali errate mostrano un errore", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("nessuno@example.invalid");
  await page.getByLabel("Password").fill("password-sbagliata");
  await page.getByRole("button", { name: "Accedi" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
});

test("intestazioni di sicurezza sulla pagina e sulle API", async ({ page, request }) => {
  const res = await page.goto("/login");
  const headers = res!.headers();
  expect(headers["content-security-policy"]).toContain("script-src 'self'");
  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  const api = await request.get("/api/auth/me");
  expect(api.status()).toBe(401);
  expect(api.headers()["cache-control"]).toBe("no-store");
});
