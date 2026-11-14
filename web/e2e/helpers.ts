import { expect, type Page } from "@playwright/test";

/** Admin credentials come from the environment (scripts/e2e.sh generates them for its isolated stack); never logged. */
export async function login(page: Page): Promise<void> {
  const email = process.env.SMT_ADMIN_EMAIL;
  const password = process.env.SMT_ADMIN_PASSWORD;
  if (!email || !password) throw new Error("SMT_ADMIN_EMAIL/SMT_ADMIN_PASSWORD mancanti: lancia i test con scripts/e2e.sh");
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Accedi" }).click();
  try {
    await expect(page.getByRole("button", { name: "Esci" })).toBeVisible({ timeout: 15_000 });
  } catch (err) {
    throw new Error(`Login non riuscito (${(err as Error).message.split("\n")[0]})`);
  }
}

export const uniqueName = () => `E2E-${Date.now()}`;

/** Creates a portfolio via the UI and lands on its detail page. */
export async function createPortfolio(page: Page, name: string, cash: string): Promise<void> {
  await page.goto("/portafogli");
  await page.getByRole("button", { name: "Nuovo portafoglio" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome", { exact: true }).fill(name);
  await dialog.getByLabel("Saldo iniziale").fill(cash);
  await dialog.getByRole("button", { name: "Crea" }).click();
  await expect(page).toHaveURL(/\/portafogli\/[0-9a-f-]{36}/);
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

// --- TOTP (live.spec.ts) -------------------------------------------------------------------
// RFC 6238 with node:crypto (no extra dependency): SHA-1, 6 digits, 30 s. The secret is the
// throwaway base32 one scripts/e2e.sh generates for the seed of its isolated stack.

function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const ch of input.replace(/=+$/, "").toUpperCase()) {
    const idx = alphabet.indexOf(ch);
    if (idx < 0) throw new Error("segreto TOTP non base32");
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

export async function totpAt(secret: string, counter: number): Promise<string> {
  const { createHmac } = await import("node:crypto");
  const msg = Buffer.alloc(8);
  msg.writeBigUInt64BE(BigInt(counter));
  const mac = createHmac("sha1", base32Decode(secret)).update(msg).digest();
  const offset = mac[mac.length - 1] & 0x0f;
  const code = (mac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

let lastTotpCounter = -1;

/** A TOTP code never used in this run: the server accepts each code once, so when the
 * current 30 s window was already spent this waits for the next one. */
export async function freshTotp(): Promise<string> {
  const secret = process.env.SMT_E2E_TOTP_SECRET;
  if (!secret) throw new Error("SMT_E2E_TOTP_SECRET mancante: lancia i test con scripts/e2e.sh");
  let counter = Math.floor(Date.now() / 30_000);
  while (counter <= lastTotpCounter) {
    await new Promise((r) => setTimeout(r, 30_000 - (Date.now() % 30_000) + 250));
    counter = Math.floor(Date.now() / 30_000);
  }
  lastTotpCounter = counter;
  return totpAt(secret, counter);
}

/** Login of the e2e admin once the live seed has enabled its TOTP. */
export async function loginWithTotp(page: Page): Promise<void> {
  const email = process.env.SMT_ADMIN_EMAIL;
  const password = process.env.SMT_ADMIN_PASSWORD;
  if (!email || !password) throw new Error("SMT_ADMIN_EMAIL/SMT_ADMIN_PASSWORD mancanti: lancia i test con scripts/e2e.sh");
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Accedi" }).click();
  await expect(page.getByLabel("Codice TOTP")).toBeVisible({ timeout: 15_000 });
  await page.getByLabel("Codice TOTP").fill(await freshTotp());
  await page.getByRole("button", { name: "Accedi" }).click();
  await expect(page.getByRole("button", { name: "Esci" })).toBeVisible({ timeout: 15_000 });
}
