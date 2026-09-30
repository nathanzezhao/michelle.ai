import { useEffect, useRef, useState } from "react";
import { useDragOrClick } from "@/hooks/useDragOrClick";
import { setIgnoreMouse } from "@/ipc/electron";

type CollapsedModeProps = {
  onExpand: () => void;
};

export function CollapsedMode({ onExpand }: CollapsedModeProps) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const draggingRef = useRef(false);
  const [pointerOnBtn, setPointerOnBtn] = useState(true);

  const syncHitTest = (over: boolean) => {
    setPointerOnBtn(over);
    setIgnoreMouse(!over);
  };

  const pointer = useDragOrClick(onExpand, {
    onDragStart: () => {
      draggingRef.current = true;
      syncHitTest(true);
    },
    onGestureEnd: () => {
      draggingRef.current = false;
    },
    onDragEnd: () => {
      syncHitTest(true);
    },
  });

  useEffect(() => {
    syncHitTest(true);
    return () => setIgnoreMouse(false);
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (draggingRef.current) return;
      const btn = btnRef.current;
      if (!btn) return;
      const over = e.target === btn || btn.contains(e.target as Node);
      if (over === pointerOnBtn) return;
      syncHitTest(over);
    };
    const onLeave = () => {
      if (draggingRef.current) return;
      syncHitTest(false);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseleave", onLeave);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseleave", onLeave);
    };
  }, [pointerOnBtn]);

  return (
    <div className="collapsed-orb-wrap">
      <button
        ref={btnRef}
        type="button"
        aria-label="Expand Michelle (drag to move)"
        onPointerDown={pointer.onPointerDown}
        onPointerUp={pointer.onPointerUp}
        onClick={pointer.onClick}
        onMouseEnter={() => syncHitTest(true)}
        onMouseLeave={() => {
          if (draggingRef.current) return;
          syncHitTest(false);
        }}
        className="collapsed-orb"
      />
    </div>
  );
}
