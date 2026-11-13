import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { arrayMove, SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react";
import { useState } from "react";
import { useSavePriority } from "@/api/hooks/connectors";
import type { ProviderPriority } from "@/api/types";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errors";
import { reorder } from "@/lib/reorder";

function Item({ id, index, last, onMove }: { id: string; index: number; last: boolean; onMove: (from: number, to: number) => void }) {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({ id });
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className="flex items-center gap-1 rounded-md border border-border bg-card px-1 py-0.5 text-sm"
    >
      <button type="button" aria-label={`Trascina ${id}`} className="cursor-grab p-1 text-muted" {...attributes} {...listeners}>
        <GripVertical className="h-4 w-4" />
      </button>
      <span className="flex-1">{id}</span>
      <Button type="button" variant="ghost" size="icon" className="h-6 w-6" aria-label={`Sposta su ${id}`} disabled={index === 0} onClick={() => onMove(index, index - 1)}>
        <ArrowUp className="h-3 w-3" />
      </Button>
      <Button type="button" variant="ghost" size="icon" className="h-6 w-6" aria-label={`Sposta giù ${id}`} disabled={last} onClick={() => onMove(index, index + 1)}>
        <ArrowDown className="h-3 w-3" />
      </Button>
    </li>
  );
}

/** One (asset class, capability) cell: a sortable provider list with its own Save. */
export function PriorityCell({ priority }: { priority: ProviderPriority }) {
  const [order, setOrder] = useState(priority.providers);
  const save = useSavePriority();
  const sensors = useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  const dirty = order.join("|") !== priority.providers.join("|");

  const move = (from: number, to: number) => setOrder((o) => reorder(o, from, to));
  function onDragEnd({ active, over }: DragEndEvent) {
    if (!over || active.id === over.id) return;
    const from = order.indexOf(String(active.id));
    const to = order.indexOf(String(over.id));
    setOrder((o) => arrayMove(o, from, to));
  }

  return (
    <div className="space-y-2">
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={order} strategy={verticalListSortingStrategy}>
          <ul className="space-y-1" aria-label={`Priorità ${priority.asset_class} ${priority.capability}`}>
            {order.map((p, i) => (
              <Item key={p} id={p} index={i} last={i === order.length - 1} onMove={move} />
            ))}
          </ul>
        </SortableContext>
      </DndContext>
      {dirty && (
        <Button
          type="button"
          size="sm"
          disabled={save.isPending}
          aria-label={`Salva priorità ${priority.asset_class} ${priority.capability}`}
          onClick={() =>
            save.mutate(
              { asset_class: priority.asset_class, capability: priority.capability, providers: order },
              { onSuccess: (data) => setOrder(data.providers) },
            )
          }
        >
          Salva
        </Button>
      )}
      {save.error && (
        <p role="alert" className="text-xs text-loss">
          {errorMessage(save.error)}
        </p>
      )}
    </div>
  );
}
