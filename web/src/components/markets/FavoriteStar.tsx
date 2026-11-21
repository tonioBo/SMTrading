import { Star } from "lucide-react";
import { useFavorites, useToggleFavorite } from "@/api/hooks/marketsV2";
import { cn } from "@/lib/utils";

/** «Aggiungi ai preferiti» / «Togli dai preferiti»: the signed-in user's own star. */
export function FavoriteStar({ symbol, className }: { symbol: string; className?: string }) {
  const favs = useFavorites();
  const toggle = useToggleFavorite();
  const on = !!favs.data?.items.some((f) => f.symbol === symbol);
  const label = on ? `Togli ${symbol} dai preferiti` : `Aggiungi ${symbol} ai preferiti`;
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={on}
      title={label}
      disabled={toggle.isPending || favs.isLoading}
      onMouseDown={(e) => e.preventDefault()}
      onClick={(e) => {
        e.stopPropagation();
        toggle.mutate({ symbol, on: !on });
      }}
      className={cn("inline-flex h-7 w-7 shrink-0 items-center justify-center rounded hover:bg-card disabled:opacity-50", className)}
    >
      <Star className={cn("h-4 w-4", on ? "fill-warn text-warn" : "text-muted")} aria-hidden />
    </button>
  );
}
