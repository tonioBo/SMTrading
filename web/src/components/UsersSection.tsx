import { Copy } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useMe } from "@/api/hooks/auth";
import { useCreateUser, useResetUserPassword, useResetUserTotp, useSetUserActive, useSetUserRole, useUsers } from "@/api/hooks/users";
import type { PortalUser, PortalUserWithPassword, UserRole } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { LiveTotpDialog } from "@/components/LiveTotpDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { ROLE_LABELS } from "@/lib/labels";

/** The temporary password, shown once: copy button and the warning it will not come back. */
export function TempPasswordDialog({ email, password, onClose }: { email: string; password: string; onClose: () => void }) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(password);
      toast.success("Password copiata");
    } catch {
      toast.error("Copia non riuscita: selezionala e copiala a mano");
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Password temporanea</DialogTitle>
          <DialogDescription>
            Per {email}. Va cambiata al primo accesso.
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <code aria-label="Password temporanea" className="flex-1 select-all rounded border border-border bg-card px-3 py-2 font-mono text-base">
            {password}
          </code>
          <Button type="button" variant="outline" size="sm" onClick={() => void copy()}>
            <Copy aria-hidden />
            Copia
          </Button>
        </div>
        <p role="alert" className="text-sm font-medium text-warn">
          Non verrà più mostrata: copiala ora e consegnala alla persona in modo sicuro.
        </p>
        <DialogFooter>
          <Button type="button" onClick={onClose}>
            Ho copiato la password
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** An action that needs the actor's TOTP when the actor has it enabled. */
type Pending =
  | { kind: "create"; email: string; role: UserRole }
  | { kind: "role"; user: PortalUser; role: UserRole }
  | { kind: "reset-password"; user: PortalUser }
  | { kind: "reset-totp"; user: PortalUser }
  | { kind: "active"; user: PortalUser; active: boolean };

function describe(p: Pending): { title: string; description: string; label: string; destructive?: boolean } {
  switch (p.kind) {
    case "create":
      return { title: `Creare ${p.email}?`, description: "Nuovo amministratore del portale.", label: "Crea" };
    case "role":
      return { title: `${p.user.email}: ${ROLE_LABELS[p.role]}?`, description: "Le sue sessioni vengono chiuse subito.", label: "Cambia ruolo" };
    case "reset-password":
      return {
        title: `Reimpostare la password di ${p.user.email}?`,
        description: "Riceverai una password temporanea da consegnare; le sue sessioni vengono chiuse subito.",
        label: "Reimposta",
      };
    case "reset-totp":
      return {
        title: `Azzerare il TOTP di ${p.user.email}?`,
        description: "Potrà accedere con la sola password e riattivarlo; le sue sessioni vengono chiuse subito.",
        label: "Azzera",
        destructive: true,
      };
    case "active":
      return p.active
        ? { title: `Riattivare ${p.user.email}?`, description: "Potrà di nuovo accedere al portale.", label: "Riattiva" }
        : { title: `Disattivare ${p.user.email}?`, description: "Non potrà più accedere; le sue sessioni vengono chiuse subito.", label: "Disattiva", destructive: true };
  }
}

function CreateUserForm({ onSubmit, pending, error }: { onSubmit: (email: string, role: UserRole) => void; pending: boolean; error: unknown }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRole>("viewer");
  function submit(e: FormEvent) {
    e.preventDefault();
    if (email.trim()) onSubmit(email.trim(), role);
  }
  const emailError = errorField(error) === "email" ? errorMessage(error) : null;
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2" aria-label="Nuovo utente" noValidate>
      <div className="w-72">
        <FormField label="Email" error={emailError}>
          <Input type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
        </FormField>
      </div>
      <div className="w-48">
        <FormField label="Ruolo">
          <select className={selectClass} value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            <option value="viewer">{ROLE_LABELS.viewer}</option>
            <option value="admin">{ROLE_LABELS.admin}</option>
          </select>
        </FormField>
      </div>
      <Button type="submit" disabled={pending || !email.trim()}>
        Crea utente
      </Button>
    </form>
  );
}

/** «Impostazioni → Utenti» (admins only): list, create, role, disable/enable, password and TOTP resets. */
export function UsersSection() {
  const me = useMe();
  const users = useUsers();
  const create = useCreateUser();
  const setRole = useSetUserRole();
  const setActive = useSetUserActive();
  const resetPassword = useResetUserPassword();
  const resetTotp = useResetUserTotp();
  const [pending, setPending] = useState<Pending | null>(null);
  const [shown, setShown] = useState<{ email: string; password: string } | null>(null);
  const actorTotp = Boolean(me.data?.totp_enabled);
  const myEmail = me.data?.email;

  const showPassword = (res: PortalUserWithPassword) => setShown({ email: res.user.email, password: res.temp_password });
  const done = (message: string) => {
    toast.success(message);
    setPending(null);
  };

  /** Does the action need the actor's TOTP (create an admin, promote, reactivate an admin,
   * reset an admin's password or TOTP)? */
  function needsTotp(p: Pending): boolean {
    if (!actorTotp) return false;
    if (p.kind === "create" || p.kind === "role") return p.role === "admin";
    if (p.kind === "reset-password" || p.kind === "reset-totp") return p.user.role === "admin";
    if (p.kind === "active") return p.active && p.user.role === "admin";
    return false;
  }

  function run(p: Pending, totp?: string) {
    const code = totp ? { totp } : {};
    switch (p.kind) {
      case "create":
        create.mutate(
          { email: p.email, role: p.role, ...code },
          {
            onSuccess: (res) => {
              setPending(null);
              showPassword(res);
            },
          },
        );
        return;
      case "role":
        setRole.mutate({ id: p.user.id, role: p.role, ...code }, { onSuccess: () => done(`${p.user.email}: ${ROLE_LABELS[p.role]}`) });
        return;
      case "reset-password":
        resetPassword.mutate(
          { id: p.user.id, ...code },
          {
            onSuccess: (res) => {
              setPending(null);
              showPassword(res);
            },
          },
        );
        return;
      case "reset-totp":
        resetTotp.mutate({ id: p.user.id, ...code }, { onSuccess: () => done(`TOTP di ${p.user.email} azzerato`) });
        return;
      case "active":
        setActive.mutate({ id: p.user.id, active: p.active, ...code }, { onSuccess: () => done(`${p.user.email} ${p.active ? "riattivato" : "disattivato"}`) });
        return;
    }
  }

  const mutation = (p: Pending | null) =>
    !p ? null : p.kind === "create" ? create : p.kind === "role" ? setRole : p.kind === "reset-password" ? resetPassword : p.kind === "reset-totp" ? resetTotp : setActive;
  const current = mutation(pending);

  function start(p: Pending) {
    for (const m of [create, setRole, setActive, resetPassword, resetTotp]) m.reset();
    if (p.kind === "create" && !needsTotp(p)) {
      run(p);
      return;
    }
    setPending(p);
  }

  const columns: Column<PortalUser>[] = [
    { key: "email", header: "Email", sortValue: (u) => u.email, cell: (u) => u.email },
    { key: "role", header: "Ruolo", sortValue: (u) => u.role, cell: (u) => ROLE_LABELS[u.role] ?? u.role },
    { key: "totp", header: "TOTP", cell: (u) => (u.totp_enabled ? "Sì" : "No") },
    { key: "login", header: "Ultimo accesso", sortValue: (u) => u.last_login_at ?? "", cell: (u) => <span className="num">{u.last_login_at ? formatDateTime(u.last_login_at) : "—"}</span> },
    {
      key: "state",
      header: "Stato",
      cell: (u) =>
        !u.active ? (
          <Badge variant="outline">Disattivato</Badge>
        ) : u.must_change_password ? (
          <Badge variant="warning">Da attivare</Badge>
        ) : (
          <Badge variant="secondary">Attivo</Badge>
        ),
    },
    {
      key: "actions",
      header: "Azioni",
      cell: (u) =>
        u.email === myEmail ? (
          <span className="text-xs text-muted">Sei tu</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            <Button type="button" size="sm" variant="outline" onClick={() => start({ kind: "role", user: u, role: u.role === "admin" ? "viewer" : "admin" })}>
              {u.role === "admin" ? "Rendi sola lettura" : "Rendi amministratore"}
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => start({ kind: "active", user: u, active: !u.active })}>
              {u.active ? "Disattiva" : "Riattiva"}
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => start({ kind: "reset-password", user: u })}>
              Reimposta password
            </Button>
            {u.totp_enabled && (
              <Button type="button" size="sm" variant="outline" onClick={() => start({ kind: "reset-totp", user: u })}>
                Azzera TOTP
              </Button>
            )}
          </div>
        ),
    },
  ];

  const text = pending ? describe(pending) : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Utenti</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted">
          Amministratore: può fare tutto. Sola lettura: vede tutto e può solo «Disattiva denaro reale» e attivare il kill-switch. Gli
          utenti non si cancellano: si disattivano.
        </p>
        <CreateUserForm onSubmit={(email, role) => start({ kind: "create", email, role })} pending={create.isPending} error={create.error} />
        {create.error && !pending && errorField(create.error) !== "email" && (
          <p role="alert" className="text-loss">
            {errorMessage(create.error)}
          </p>
        )}
        {users.isError && <p className="text-loss">Impossibile caricare gli utenti</p>}
        <DataTable aria-label="Utenti" columns={columns} rows={users.data} rowKey={(u) => u.id} loading={users.isLoading} empty="Nessun utente" />
      </CardContent>
      {pending && text && current && needsTotp(pending) && (
        <LiveTotpDialog
          title={text.title}
          description={`${text.description} Conferma con il tuo codice TOTP.`}
          confirmLabel={text.label}
          destructive={text.destructive}
          pending={current.isPending}
          error={current.error}
          onClose={() => setPending(null)}
          onConfirm={(totp) => run(pending, totp)}
        />
      )}
      {pending && text && current && !needsTotp(pending) && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setPending(null)}
          title={text.title}
          description={text.description}
          confirmLabel={text.label}
          destructive={text.destructive}
          pending={current.isPending}
          error={current.error ? errorMessage(current.error) : null}
          onConfirm={() => run(pending)}
        />
      )}
      {shown && <TempPasswordDialog email={shown.email} password={shown.password} onClose={() => setShown(null)} />}
    </Card>
  );
}
