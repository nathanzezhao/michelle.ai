import { useCallback, useEffect, useRef } from "react";
import { dragStart, dragStop } from "@/ipc/electron";
import { isClickNotDrag } from "@/lib/pointerDrag";

type DragOrClickOptions = {
  onDragStart?: () => void;
  onDragEnd?: () => void;
  onGestureEnd?: () => void;
};

export function useDragOrClick(onClick: () => void, options: DragOrClickOptions = {}) {
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const draggingRef = useRef(false);
  const onDragStart = options.onDragStart;
  const onDragEnd = options.onDragEnd;
  const onGestureEnd = options.onGestureEnd;

  const stopGesture = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    dragStop();
    onGestureEnd?.();
  }, [onGestureEnd]);

  useEffect(() => () => stopGesture(), [stopGesture]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      draggingRef.current = true;
      onDragStart?.();
      startRef.current = { x: event.screenX, y: event.screenY };
      event.currentTarget.setPointerCapture(event.pointerId);
      dragStart(event.screenX, event.screenY);
    },
    [onDragStart]
  );

  const onPointerUp = useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      if (!draggingRef.current) return;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      const start = startRef.current;
      startRef.current = null;
      stopGesture();
      if (!start) return;
      if (isClickNotDrag(start, { x: event.screenX, y: event.screenY })) {
        onClick();
        return;
      }
      onDragEnd?.();
    },
    [onClick, onDragEnd, stopGesture]
  );

  const onKeyboardClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      if (event.detail !== 0) return;
      onClick();
    },
    [onClick]
  );

  return { onPointerDown, onPointerUp, onClick: onKeyboardClick };
}
