// THE SACRED RULE — reserved incandescent (#F6FFE1), ≥10× winners only.
// Returns the "ignite" class when a position's multiple has crossed the tail
// threshold; nothing below 10x may ever wear the reserved color.
export const IGNITE_MIN = 10;
export const igniteClass = (multiple) =>
  Number(multiple) >= IGNITE_MIN ? "ignite" : "";
