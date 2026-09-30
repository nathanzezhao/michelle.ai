export const DRAG_CLICK_SLOP_PX = 5;

export function isClickNotDrag(
  start: { x: number; y: number },
  end: { x: number; y: number },
  slopPx = DRAG_CLICK_SLOP_PX
): boolean {
  return Math.abs(end.x - start.x) < slopPx && Math.abs(end.y - start.y) < slopPx;
}
