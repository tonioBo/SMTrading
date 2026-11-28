import QRCode from "qrcode";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useIsAdmin } from "@/api/hooks/auth";
import {
  TV_PAIRING_S,
  useAddTv,
  useAdminRevokeTv,
  useAllTvs,
  useDiscoverTvs,
  usePairTv,
  usePowerOffTv,
  useRemoveTv,
  useRevokeTvLink,
  useRotateTvLink,
  useSaveTvDisplay,
  useSendToTv,
  useTvArea,
  useTvPower,
} from "@/api/hooks/tv";
import type { Tv, TvArea, TvScreenKey, TvSendResult } from "@/api/tvTypes";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

/** «Schermo TV»: each user's own area — their TVs, what they show, sounds, the display
 * link. Admins also search the network and see (and revoke) every user's TVs. */

export const PAIR_HINT = "Sulla TV comparirà una richiesta: accettala con il telecomando.";
export const NEED_HTTPS = "Serve l'indirizzo https del portale: la TV apre le schermate solo in modo sicuro. Scrivilo qui sotto (per esempio https://192.168.1.10).";

/** the TV page needs https (its session cookie is Secure): an http address is never shown */
const isHttps = (url: string | null | undefined): url is string => !!url && url.startsWith("https://");

const POWER_TEXT: Record<string, string> = {
  on: "accesa",
  standby: "in standby",
  unreachable: "non raggiungibile",
  not_samsung: "non risponde come una TV Samsung",
};

function useQrDataUrl(text: string | null): string | null {
  const [qr, setQr] = useState<{ text: string; data: string } | null>(null);
  useEffect(() => {
    if (!text) return;
    let stale = false;
    QRCode.toDataURL(text, { margin: 1, width: 240 })
      .then((data) => {
        if (!stale) setQr({ text, data });
      })
      .catch(() => {
        // the address in text is enough
      });
    return () => {
      stale = true;
    };
  }, [text]);
  return text && qr?.text === text ? qr.data : null;
}

/** the display address as large text and as a QR code (to open it by hand on the TV) */
export function LinkFallback({ url }: { url: string }) {
  const qr = useQrDataUrl(url);
  return (
    <div className="flex flex-wrap items-start gap-4 rounded-md border border-border p-3">
      {qr && <img src={qr} alt="Codice QR del link della TV" width={160} height={160} className="rounded bg-white p-1" />}
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm text-muted">Apri questo indirizzo nel browser della TV (basta una volta) o inquadra il codice con il telefono:</p>
        <p className="break-all font-mono text-lg font-semibold" data-testid="tv-link-url">
          {url}
        </p>
        <p className="text-xs text-muted">Il link apre solo le schermate TV, non il resto del portale. Non condividerlo.</p>
      </div>
    </div>
  );
}

function Message({ tone, children }: { tone: "ok" | "warn" | "bad"; children: ReactNode }) {
  const cls = tone === "ok" ? "text-gain" : tone === "bad" ? "text-loss" : "text-amber-500";
  return (
    <p role={tone === "ok" ? "status" : "alert"} className={`text-sm ${cls}`}>
      {children}
    </p>
  );
}

function PairingProgress({ tv }: { tv: Tv }) {
  const started = tv.pairing.started_at ? Date.parse(tv.pairing.started_at) : Date.now();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);
  const elapsed = Math.max(0, Math.min(TV_PAIRING_S, Math.round((now - started) / 1000)));
  return (
    <div className="space-y-1" role="status" aria-label="Collegamento in corso">
      <p className="text-sm font-medium">{PAIR_HINT}</p>
      <p className="text-sm text-muted">
        In attesa della risposta sulla TV… {elapsed} s su {TV_PAIRING_S}
      </p>
      <progress className="h-2 w-full" max={TV_PAIRING_S} value={elapsed} aria-label="Attesa della risposta" />
    </div>
  );
}

function pairingOutcome(tv: Tv): { tone: "ok" | "warn" | "bad"; text: string } | null {
  const m = tv.pairing.message;
  switch (tv.pairing.state) {
    case "ok":
      return { tone: "ok", text: m ?? "TV collegata." };
    case "refused":
      return { tone: "bad", text: m ?? "La richiesta è stata rifiutata sulla TV. Premi di nuovo «Collega» e scegli «Consenti»." };
    case "timeout":
      return { tone: "warn", text: m ?? "Nessuna risposta dalla TV entro 30 secondi: controlla che sia accesa e riprova." };
    case "error":
      return { tone: "bad", text: m ?? "Collegamento non riuscito." };
    default:
      return null;
  }
}

interface Choice {
  screen: TvScreenKey;
  rotation: boolean;
  interval_s: number;
}

function TvRow({ tv, choice, linkUrl }: { tv: Tv; choice: Choice; linkUrl: string | null }) {
  const pair = usePairTv();
  const send = useSendToTv();
  const off = usePowerOffTv();
  const remove = useRemoveTv();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [sent, setSent] = useState<TvSendResult | null>(null);
  const [offMsg, setOffMsg] = useState<{ ok: boolean; message: string } | null>(null);
  const revoked = tv.revoked_at !== null;
  const waiting = tv.pairing.state === "waiting";
  const power = useTvPower(tv.id, !revoked && !waiting);
  const outcome = waiting ? null : pairingOutcome(tv);
  const candidate = sent && (!sent.ok || !sent.confirmed) ? sent.url || linkUrl : null;
  const fallbackUrl = isHttps(candidate) ? candidate : null;

  return (
    <li className="space-y-3 rounded-md border border-border p-4" aria-label={tv.name}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{tv.name}</span>
        <span className="font-mono text-sm text-muted">{tv.host}</span>
        {tv.model && <span className="text-sm text-muted">{tv.model}</span>}
        {revoked ? (
          <Badge variant="destructive">revocata</Badge>
        ) : tv.paired ? (
          <Badge>collegata</Badge>
        ) : (
          <Badge variant="secondary">da collegare</Badge>
        )}
        {power.data && <span className="text-sm text-muted">· {POWER_TEXT[power.data.power] ?? power.data.power}</span>}
        {tv.last_used_at && <span className="text-xs text-muted">· ultimo invio {formatDateTime(tv.last_used_at)}</span>}
      </div>
      {revoked && <Message tone="warn">Un amministratore ha revocato questa TV: premi «Collega» per collegarla di nuovo.</Message>}
      {waiting && <PairingProgress tv={tv} />}
      {outcome && !(outcome.tone === "ok" && sent) && <Message tone={outcome.tone}>{outcome.text}</Message>}
      {pair.error != null && <Message tone="bad">{errorMessage(pair.error)}</Message>}
      <div className="flex flex-wrap gap-2">
        <Button variant={tv.paired ? "outline" : "default"} disabled={waiting || pair.isPending} onClick={() => pair.mutate(tv.id)}>
          {tv.paired ? "Collega di nuovo" : "Collega"}
        </Button>
        <Button
          disabled={!tv.paired || revoked || waiting || send.isPending}
          onClick={() => {
            setSent(null);
            send.mutate({ id: tv.id, ...choice }, { onSuccess: setSent });
          }}
        >
          {send.isPending ? "Invio in corso…" : "Manda alla TV"}
        </Button>
        <Button
          variant="outline"
          disabled={!tv.paired || revoked || off.isPending}
          onClick={() => {
            setOffMsg(null);
            off.mutate(tv.id, { onSuccess: setOffMsg });
          }}
        >
          Spegni lo schermo
        </Button>
        <Button variant="ghost" onClick={() => setConfirmRemove(true)}>
          Togli
        </Button>
      </div>
      {!tv.paired && !waiting && !revoked && <p className="text-xs text-muted">Prima di mandare le schermate collega la TV: {PAIR_HINT.toLowerCase()}</p>}
      {send.error != null && <Message tone="bad">{errorMessage(send.error)}</Message>}
      {sent && <Message tone={sent.ok && sent.confirmed ? "ok" : "warn"}>{sent.message}</Message>}
      {fallbackUrl && <LinkFallback url={fallbackUrl} />}
      {offMsg && <Message tone={offMsg.ok ? "ok" : "warn"}>{offMsg.message}</Message>}
      {off.error != null && <Message tone="bad">{errorMessage(off.error)}</Message>}
      <ConfirmDialog
        open={confirmRemove}
        onOpenChange={setConfirmRemove}
        title={`Togliere «${tv.name}»?`}
        description="SMTrading dimentica la TV e il suo collegamento. Il link della TV resta valido finché non lo revochi."
        confirmLabel="Togli"
        destructive
        pending={remove.isPending}
        error={remove.error != null ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate(tv.id, { onSuccess: () => setConfirmRemove(false) })}
      />
    </li>
  );
}

function AddTv({ area }: { area: TvArea }) {
  const add = useAddTv();
  const discover = useDiscoverTvs();
  const [host, setHost] = useState("");
  const [name, setName] = useState("");
  const [subnet, setSubnet] = useState(area.suggested_subnet ?? "");
  const known = new Set(area.tvs.map((t) => t.host));
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!host.trim()) return;
    add.mutate({ host: host.trim(), ...(name.trim() ? { name: name.trim() } : {}) }, { onSuccess: () => (setHost(""), setName("")) });
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Aggiungi una TV</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <form onSubmit={submit} className="flex flex-wrap items-end gap-3" noValidate>
          <div className="w-56">
            <FormField label="Indirizzo della TV" hint="per esempio 192.168.1.50">
              <Input value={host} onChange={(e) => setHost(e.target.value)} inputMode="decimal" autoComplete="off" />
            </FormField>
          </div>
          <div className="w-56">
            <FormField label="Nome (facoltativo)">
              <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
            </FormField>
          </div>
          <Button type="submit" disabled={add.isPending || !host.trim()}>
            Aggiungi
          </Button>
        </form>
        {add.error != null && <Message tone="bad">{errorMessage(add.error)}</Message>}
        {area.can_discover && (
          <div className="space-y-3 border-t border-border pt-4">
            <div className="flex flex-wrap items-end gap-3">
              <div className="w-56">
                <FormField label="Rete da cercare" hint="vuoto: la rete del portale">
                  <Input value={subnet} onChange={(e) => setSubnet(e.target.value)} placeholder="192.168.1.0/24" autoComplete="off" />
                </FormField>
              </div>
              <Button variant="outline" disabled={discover.isPending} onClick={() => discover.mutate(subnet.trim() || null)}>
                {discover.isPending ? "Ricerca in corso…" : "Cerca TV in rete"}
              </Button>
            </div>
            {discover.error != null && <Message tone="bad">{errorMessage(discover.error)}</Message>}
            {discover.data && (
              <div aria-label="TV trovate" role="region" className="space-y-2">
                {discover.data.devices.length === 0 ? (
                  <p className="text-sm text-muted">Nessuna TV Samsung trovata in {discover.data.subnet}.</p>
                ) : (
                  <ul className="space-y-2">
                    {discover.data.devices.map((d) => (
                      <li key={d.host} className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-semibold">{d.name}</span>
                        <span className="font-mono text-muted">{d.host}</span>
                        {d.model && <span className="text-muted">{d.model}</span>}
                        <span className="text-muted">· {d.power === "on" ? "accesa" : "in standby"}</span>
                        {known.has(d.host) ? (
                          <Badge variant="secondary">già aggiunta</Badge>
                        ) : (
                          <Button size="sm" variant="outline" disabled={add.isPending} onClick={() => add.mutate({ host: d.host, name: d.name })}>
                            Aggiungi {d.name}
                          </Button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ScreenChoice({ area, choice, setChoice }: { area: TvArea; choice: Choice; setChoice: (c: Choice) => void }) {
  const save = useSaveTvDisplay();
  const [saved, setSaved] = useState(false);
  const { min, max } = area.interval;
  const intervalOk = Number.isInteger(choice.interval_s) && choice.interval_s >= min && choice.interval_s <= max;
  const dirty = choice.screen !== area.display.screen || choice.rotation !== area.display.rotation || choice.interval_s !== area.display.interval_s;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cosa mostrare</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <fieldset className="space-y-2">
          <legend className="mb-1 text-sm font-medium">Schermata</legend>
          {area.screens.map((s) => (
            <label key={s.key} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="tv-screen"
                value={s.key}
                checked={choice.screen === s.key}
                onChange={() => (setSaved(false), setChoice({ ...choice, screen: s.key }))}
              />
              {s.label}
            </label>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={choice.rotation} onCheckedChange={(b) => (setSaved(false), setChoice({ ...choice, rotation: b }))} aria-label="Rotazione" />
            Rotazione tra le schermate
          </label>
          {choice.rotation && (
            <div className="w-40">
              <FormField label="Ogni (secondi)" error={intervalOk ? null : `da ${min} a ${max} secondi`}>
                <Input
                  type="number"
                  min={min}
                  max={max}
                  value={Number.isFinite(choice.interval_s) ? choice.interval_s : ""}
                  onChange={(e) => (setSaved(false), setChoice({ ...choice, interval_s: e.target.valueAsNumber }))}
                />
              </FormField>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            disabled={!dirty || !intervalOk || save.isPending}
            onClick={() => save.mutate({ screen: choice.screen, rotation: choice.rotation, interval_s: choice.interval_s }, { onSuccess: () => setSaved(true) })}
          >
            Cambia sulla TV
          </Button>
          <span className="text-xs text-muted">Una TV che mostra già SMTrading cambia da sola entro pochi secondi; non viene mai accesa da qui.</span>
        </div>
        {saved && !dirty && <Message tone="ok">Scelta salvata.</Message>}
        {save.error != null && <Message tone="bad">{errorMessage(save.error)}</Message>}
      </CardContent>
    </Card>
  );
}

function Sounds({ area }: { area: TvArea }) {
  const save = useSaveTvDisplay();
  const [enabled, setEnabled] = useState(area.sound.enabled);
  const [volume, setVolume] = useState(area.sound.volume);
  const [start, setStart] = useState(area.sound.quiet_start ?? "");
  const [end, setEnd] = useState(area.sound.quiet_end ?? "");
  const [saved, setSaved] = useState(false);
  const halfQuiet = (start === "") !== (end === "");
  return (
    <Card>
      <CardHeader>
        <CardTitle>Suoni</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted">
          Pochi suoni, solo per fatti veri: un rintocco per un ordine eseguito (diverso per acquisto e vendita), due note quando scatta uno stop o un
          obiettivo, un avviso basso quando qualcosa diventa rosso nella salute e uno di «tutto a posto» quando rientra. Al massimo un suono ogni pochi
          secondi.
        </p>
        <label className="flex items-center gap-2">
          <Switch checked={enabled} onCheckedChange={(b) => (setSaved(false), setEnabled(b))} aria-label="Suoni accesi" />
          {enabled ? "Suoni accesi" : "Suoni spenti"}
        </label>
        <div className="flex flex-wrap items-end gap-4">
          <div className="w-56">
            <FormField label={`Volume: ${volume}`}>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={volume}
                disabled={!enabled}
                onChange={(e) => (setSaved(false), setVolume(Number(e.target.value)))}
                className="w-full"
              />
            </FormField>
          </div>
          <div className="w-32">
            <FormField label="Silenzio dalle">
              <Input type="time" value={start} disabled={!enabled} onChange={(e) => (setSaved(false), setStart(e.target.value))} />
            </FormField>
          </div>
          <div className="w-32">
            <FormField label="alle">
              <Input type="time" value={end} disabled={!enabled} onChange={(e) => (setSaved(false), setEnd(e.target.value))} />
            </FormField>
          </div>
        </div>
        <p className="text-xs text-muted">
          Ore di silenzio in ora di Roma: in quelle ore la TV non suona. Lasciale vuote entrambe per non averne.
          {area.sound.quiet_now && area.sound.enabled && " Adesso è in corso il silenzio."}
        </p>
        <p className="text-xs text-muted">Se la TV blocca l&apos;audio, sulla schermata compare «premi OK sul telecomando per attivare i suoni».</p>
        {halfQuiet && <Message tone="warn">Indica sia l&apos;inizio sia la fine del silenzio, oppure nessuno dei due.</Message>}
        <Button
          disabled={save.isPending || halfQuiet}
          onClick={() =>
            save.mutate(
              { sound_enabled: enabled, sound_volume: volume, quiet_start: start || null, quiet_end: end || null },
              { onSuccess: () => setSaved(true) },
            )
          }
        >
          Salva i suoni
        </Button>
        {saved && <Message tone="ok">Suoni salvati.</Message>}
        {save.error != null && <Message tone="bad">{errorMessage(save.error)}</Message>}
      </CardContent>
    </Card>
  );
}

function DisplayLink({ area }: { area: TvArea }) {
  const rotate = useRotateTvLink();
  const revoke = useRevokeTvLink();
  const save = useSaveTvDisplay();
  const [confirm, setConfirm] = useState<"rotate" | "revoke" | null>(null);
  const [portal, setPortal] = useState(area.display.portal_url ?? "");
  const noAddress = !area.display.portal_url && !area.portal_url_detected;
  const { link } = area;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Link della TV</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted">
          La TV apre le schermate con un link personale, di sola lettura, senza password. «Manda alla TV» lo usa da solo; se non riesce, aprilo a mano
          sulla TV una volta.
        </p>
        {link.active && isHttps(link.url) && <LinkFallback url={link.url} />}
        {link.active && !isHttps(link.url) && (
          <Message tone="warn">
            {link.reason || (link.url ? NEED_HTTPS : "Il link c'è, ma manca l'indirizzo del portale visto dalla TV: scrivilo qui sotto.")}
          </Message>
        )}
        {!link.active && <p>Nessun link attivo: la TV non può aprire le schermate.</p>}
        <div className="flex flex-wrap gap-2">
          <Button variant={link.active ? "outline" : "default"} disabled={rotate.isPending} onClick={() => (link.active ? setConfirm("rotate") : rotate.mutate())}>
            {link.active ? "Nuovo link" : "Crea il link"}
          </Button>
          {link.active && (
            <Button variant="destructive" onClick={() => setConfirm("revoke")}>
              Revoca il link
            </Button>
          )}
        </div>
        {rotate.error != null && <Message tone="bad">{errorMessage(rotate.error)}</Message>}
        <form
          className="flex flex-wrap items-end gap-3 border-t border-border pt-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate({ portal_url: portal.trim() });
          }}
        >
          <div className="w-80">
            <FormField label="Indirizzo del portale visto dalla TV" hint={area.portal_url_detected ? `vuoto: ${area.portal_url_detected}` : "per esempio https://192.168.1.10"}>
              <Input value={portal} onChange={(e) => setPortal(e.target.value)} placeholder={area.portal_url_detected ?? "https://…"} autoComplete="off" />
            </FormField>
          </div>
          <Button type="submit" variant="outline" disabled={save.isPending || portal.trim() === (area.display.portal_url ?? "")}>
            Salva l&apos;indirizzo
          </Button>
        </form>
        {noAddress && (
          <Message tone="warn">Il portale è aperto da questo computer: scrivi l&apos;indirizzo di rete del portale, così la TV può raggiungerlo.</Message>
        )}
        {save.error != null && <Message tone="bad">{errorMessage(save.error)}</Message>}
        <ConfirmDialog
          open={confirm !== null}
          onOpenChange={(o) => !o && setConfirm(null)}
          title={confirm === "revoke" ? "Revocare il link della TV?" : "Creare un nuovo link?"}
          description={
            confirm === "revoke"
              ? "La TV smette subito di mostrare le schermate. Per riaverle serve un nuovo link."
              : "Il link attuale smette subito di funzionare: la TV va aperta di nuovo con «Manda alla TV» o con il nuovo indirizzo."
          }
          confirmLabel={confirm === "revoke" ? "Revoca" : "Nuovo link"}
          destructive={confirm === "revoke"}
          pending={rotate.isPending || revoke.isPending}
          error={revoke.error != null ? errorMessage(revoke.error) : null}
          onConfirm={() => {
            const done = { onSuccess: () => setConfirm(null) };
            if (confirm === "revoke") revoke.mutate(undefined, done);
            else rotate.mutate(undefined, done);
          }}
        />
      </CardContent>
    </Card>
  );
}

function AllTvs() {
  const all = useAllTvs(true);
  const revoke = useAdminRevokeTv();
  const [target, setTarget] = useState<{ id: string; name: string; email: string } | null>(null);
  return (
    <Card>
      <CardHeader>
        <CardTitle>TV di tutti gli utenti</CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        {all.isLoading && <p className="text-muted">Caricamento…</p>}
        {all.error != null && <Message tone="bad">{errorMessage(all.error)}</Message>}
        {all.data && all.data.items.length === 0 && <p className="text-muted">Nessuna TV aggiunta.</p>}
        {all.data && all.data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left" aria-label="TV di tutti gli utenti">
              <thead className="text-xs text-muted">
                <tr>
                  <th className="py-1 pr-3 font-normal">Utente</th>
                  <th className="py-1 pr-3 font-normal">TV</th>
                  <th className="py-1 pr-3 font-normal">Indirizzo</th>
                  <th className="py-1 pr-3 font-normal">Stato</th>
                  <th className="py-1 pr-3 font-normal">Ultimo uso</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {all.data.items.map((t) => (
                  <tr key={t.id} className="border-t border-border">
                    <td className="py-2 pr-3">{t.user_email}</td>
                    <td className="py-2 pr-3">{t.name}</td>
                    <td className="py-2 pr-3 font-mono">{t.host}</td>
                    <td className="py-2 pr-3">{t.revoked_at ? "revocata" : t.paired ? "collegata" : "da collegare"}</td>
                    <td className="py-2 pr-3">{t.last_used_at ? formatDateTime(t.last_used_at) : "—"}</td>
                    <td className="py-2 text-right">
                      {!t.revoked_at && (
                        <Button size="sm" variant="outline" onClick={() => setTarget({ id: t.id, name: t.name, email: t.user_email })}>
                          Revoca
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ConfirmDialog
          open={target !== null}
          onOpenChange={(o) => !o && setTarget(null)}
          title={target ? `Revocare «${target.name}» di ${target.email}?` : ""}
          description="La TV perde il collegamento e il link di quell'utente smette subito di funzionare."
          confirmLabel="Revoca"
          destructive
          pending={revoke.isPending}
          error={revoke.error != null ? errorMessage(revoke.error) : null}
          onConfirm={() => target && revoke.mutate(target.id, { onSuccess: () => setTarget(null) })}
        />
      </CardContent>
    </Card>
  );
}

function Area({ area }: { area: TvArea }) {
  const admin = useIsAdmin();
  const [choice, setChoice] = useState<Choice>({ screen: area.display.screen, rotation: area.display.rotation, interval_s: area.display.interval_s });
  // a selection saved elsewhere (another tab) comes in when this one is not being edited
  const version = area.display.version;
  useEffect(() => {
    setChoice({ screen: area.display.screen, rotation: area.display.rotation, interval_s: area.display.interval_s });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);
  return (
    <div className="space-y-6">
      <p className="max-w-3xl text-sm text-muted">{area.network_note}</p>
      <Card>
        <CardHeader>
          <CardTitle>Le tue TV</CardTitle>
        </CardHeader>
        <CardContent>
          {area.tvs.length === 0 ? (
            <p className="text-sm text-muted">Nessuna TV: aggiungila qui sotto con il suo indirizzo{area.can_discover ? " o cercala in rete" : ""}.</p>
          ) : (
            <ul className="space-y-3">
              {area.tvs.map((tv) => (
                <TvRow key={tv.id} tv={tv} choice={choice} linkUrl={area.link.url} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <AddTv area={area} />
      <ScreenChoice area={area} choice={choice} setChoice={setChoice} />
      <Sounds area={area} />
      <DisplayLink area={area} />
      {admin && <AllTvs />}
    </div>
  );
}

export default function TvScreens() {
  const area = useTvArea();
  return (
    <div>
      <PageHeader title="Schermo TV" />
      {area.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {area.error != null && <Message tone="bad">{errorMessage(area.error)}</Message>}
      {area.data && <Area area={area.data} />}
    </div>
  );
}
