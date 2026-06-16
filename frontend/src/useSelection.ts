import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Multi-select state shared by the list pages (Rules, Digests, Categories):
 * a Set of selected ids, derived all/some-checked flags over the current
 * `items`, and a ref wired to the header checkbox's indeterminate state.
 */
export function useSelection<T>(items: T[], getId: (item: T) => number) {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const ids = items.map(getId);
  const allChecked = ids.length > 0 && ids.every((id) => selectedIds.has(id));
  const someChecked = ids.some((id) => selectedIds.has(id));

  const selectAllRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selectAllRef.current)
      selectAllRef.current.indeterminate = someChecked && !allChecked;
  }, [someChecked, allChecked]);

  const toggle = useCallback(
    (id: number) =>
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [],
  );
  const selectAll = useCallback(
    () => setSelectedIds(new Set(items.map(getId))),
    [items, getId],
  );
  const clear = useCallback(() => setSelectedIds(new Set()), []);

  return { selectedIds, allChecked, someChecked, selectAllRef, toggle, selectAll, clear };
}
