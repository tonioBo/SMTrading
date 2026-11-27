import { FormField } from "@/components/FormField";
import { Input } from "@/components/ui/input";
import { BRAKES_HELP, type BrakeSpec } from "@/lib/autoRules";

/** The brake fields of a rule or of a portfolio: a blank one switches the brake off. */
export function AutoBrakeFields({
  specs,
  values,
  errors,
  onChange,
}: {
  specs: BrakeSpec[];
  values: Record<string, string>;
  errors: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <p className="text-xs text-muted sm:col-span-2">{BRAKES_HELP}</p>
      {specs.map((s) => (
        <FormField key={s.key} label={s.label} hint={s.help} error={errors[s.key]}>
          <Input value={values[s.key] ?? ""} inputMode="decimal" onChange={(e) => onChange(s.key, e.target.value)} />
        </FormField>
      ))}
    </div>
  );
}
