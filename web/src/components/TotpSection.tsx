import QRCode from "qrcode";
import { useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useMe, useTotpSetup, useTotpToggle } from "@/api/hooks/auth";
import { FormField } from "@/components/FormField";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

const CODE_RE = /^\d{6}$/;

/** The otpauth:// URI as a QR code (data: URL, generated in the browser). */
function useQrDataUrl(uri: string | undefined): string | null {
  const [url, setUrl] = useState<{ uri: string; data: string } | null>(null);
  useEffect(() => {
    if (!uri) return;
    let stale = false;
    QRCode.toDataURL(uri, { margin: 1, width: 192 })
      .then((data) => {
        if (!stale) setUrl({ uri, data });
      })
      .catch(() => {
        // the secret text below is enough to configure the app
      });
    return () => {
      stale = true;
    };
  }, [uri]);
  return uri && url?.uri === uri ? url.data : null;
}

function CodeForm({ label, pending, error, onSubmit }: { label: string; pending: boolean; error: unknown; onSubmit: (code: string) => void }) {
  const [code, setCode] = useState("");
  const [touched, setTouched] = useState(false);
  const invalid = touched && !CODE_RE.test(code) ? "Inserisci le 6 cifre del codice" : null;
  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (CODE_RE.test(code)) onSubmit(code);
  }
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2" noValidate>
      <div className="w-48">
        <FormField label="Codice dall'app" error={invalid}>
          <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\s/g, ""))} />
        </FormField>
      </div>
      <Button type="submit" disabled={pending}>
        {label}
      </Button>
      {error != null && (
        <p role="alert" className="w-full text-sm text-loss">
          {errorMessage(error)}
        </p>
      )}
    </form>
  );
}

export function TotpSection() {
  const me = useMe();
  const setup = useTotpSetup();
  const enable = useTotpToggle("enable");
  const disable = useTotpToggle("disable");
  const qr = useQrDataUrl(setup.data?.otpauth_uri);
  const enabled = me.data?.totp_enabled ?? false;

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Autenticazione a due fattori</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex items-center gap-2">
          Stato: <Badge variant={enabled ? "default" : "secondary"}>{enabled ? "attiva" : "non attiva"}</Badge>
        </div>
        {me.data && !enabled && !setup.data && (
          <>
            <p className="text-muted">Al login verrà chiesto anche il codice a 6 cifre di un&apos;app di autenticazione (Google Authenticator, Aegis, 1Password…).</p>
            <Button type="button" onClick={() => setup.mutate()} disabled={setup.isPending}>
              Configura
            </Button>
            {setup.error && (
              <p role="alert" className="text-loss">
                {errorMessage(setup.error)}
              </p>
            )}
          </>
        )}
        {me.data && !enabled && setup.data && (
          <>
            <p>Inquadra il codice QR con l&apos;app di autenticazione, oppure inserisci a mano la chiave, poi conferma con il codice generato.</p>
            {qr && <img src={qr} alt="Codice QR per l'app di autenticazione" width={192} height={192} className="rounded bg-white p-1" />}
            <p>
              Chiave: <code className="num select-all break-all">{setup.data.secret}</code>
            </p>
            <CodeForm
              label="Attiva"
              pending={enable.isPending}
              error={enable.error}
              onSubmit={(code) =>
                enable.mutate(code, {
                  onSuccess: () => {
                    setup.reset();
                    toast.success("Autenticazione a due fattori attivata. Le altre sessioni sono state chiuse.");
                  },
                })
              }
            />
          </>
        )}
        {enabled && (
          <>
            <p className="text-muted">Per disattivarla inserisci un codice valido dell&apos;app.</p>
            <CodeForm
              label="Disattiva"
              pending={disable.isPending}
              error={disable.error}
              onSubmit={(code) =>
                disable.mutate(code, {
                  onSuccess: () => {
                    disable.reset();
                    toast.success("Autenticazione a due fattori disattivata. Le altre sessioni sono state chiuse.");
                  },
                })
              }
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}
