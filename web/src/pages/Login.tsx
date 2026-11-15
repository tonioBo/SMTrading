import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { useLogin } from "@/api/hooks/auth";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/Logo";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { safeNext } from "@/lib/nav";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Accesso non riuscito. Riprova.";
}

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [totpRequired, setTotpRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const login = useLogin();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    login.mutate(
      { email, password, ...(totpRequired && totp ? { totp } : {}) },
      {
        onSuccess: () => navigate(safeNext(params.get("next")), { replace: true }),
        onError: (err) => {
          const details = err instanceof ApiError ? (err.details as { totp_required?: boolean } | null) : null;
          if (details?.totp_required) {
            const firstPrompt = !totpRequired;
            setTotpRequired(true);
            setError(firstPrompt ? "Inserisci il codice TOTP dell'app di autenticazione." : errorMessage(err));
            return;
          }
          setError(errorMessage(err));
        },
      },
    );
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-2xl">
            <Logo className="h-8 w-8" />
            <span>
              <span className="text-accent">SM</span>Trading
            </span>
          </CardTitle>
          <CardDescription>Accedi al pannello di amministrazione</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
            <div className="flex flex-col gap-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {totpRequired && (
              <div className="flex flex-col gap-2">
                <Label htmlFor="totp">Codice TOTP</Label>
                <Input
                  id="totp"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  autoFocus
                  className="num"
                  value={totp}
                  onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                />
              </div>
            )}
            {error && (
              <p role="alert" className="text-sm text-loss">
                {error}
              </p>
            )}
            <Button type="submit" disabled={login.isPending || !email || !password}>
              {login.isPending ? "Accesso in corso…" : "Accedi"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
