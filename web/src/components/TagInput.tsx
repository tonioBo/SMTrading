import { X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import { Input } from "@/components/ui/input";

interface Props {
  id?: string;
  value: string[];
  onChange: (tags: string[]) => void;
  placeholder?: string;
  "aria-label"?: string;
}

/** A list of short strings edited as tags: Invio or virgola adds, the x removes. */
export function TagInput({ id, value, onChange, placeholder, "aria-label": ariaLabel }: Props) {
  const [draft, setDraft] = useState("");

  function commit(text: string = draft) {
    // a pasted "AAPL, MSFT NVDA" adds every value
    const tags = text.split(/[\s,;]+/).map((t) => t.trim()).filter(Boolean);
    const next = [...value];
    for (const tag of tags) if (!next.includes(tag)) next.push(tag);
    if (next.length !== value.length) onChange(next);
    setDraft("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1));
    }
  }

  return (
    <div className="space-y-2">
      {value.length > 0 && (
        <ul className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">
          {value.map((tag) => (
            <li key={tag} className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-mono text-xs">
              {tag}
              <button
                type="button"
                aria-label={`Rimuovi ${tag}`}
                className="text-muted hover:text-fg"
                onClick={() => onChange(value.filter((t) => t !== tag))}
              >
                <X className="h-3 w-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <Input
        id={id}
        aria-label={ariaLabel}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => commit()}
        onPaste={(e) => {
          const text = e.clipboardData.getData("text");
          if (/[\s,;]/.test(text.trim())) {
            e.preventDefault();
            commit(draft + text);
          }
        }}
      />
    </div>
  );
}
