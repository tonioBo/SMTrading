import type { ReactNode } from "react";

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <h1 className="min-w-0 break-words text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
      {children}
    </div>
  );
}
