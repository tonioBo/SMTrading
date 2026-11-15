/** The yellow note under a name field after a save: the name has a word that would give
 * the simulation away to the agents (only a warning, the save is never blocked). */
export function NameWarning({ warning }: { warning?: string | null }) {
  if (!warning) return null;
  return (
    <p role="status" className="text-xs text-warn">
      {warning}
    </p>
  );
}
