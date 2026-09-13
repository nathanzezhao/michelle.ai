import { useEffect, useRef, useState } from "react";
import { dragStart, dragStop, setIgnoreMouse } from "@/ipc/electron";

type CollapsedModeProps = {
  onExpand: () => void;
};

export function CollapsedMode({ onExpand }: CollapsedModeProps) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const [pointerOnBtn, setPointerOnBtn] = useState(true);
  const dragStartScreen = useRef<{ x: number; y: number } | null>(null);

  const syncHitTest = (over: boolean) => {
    setPointerOnBtn(over);
    setIgnoreMouse(!over);
  };

  useEffect(() => {
    syncHitTest(true);
    return () => setIgnoreMouse(false);
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const btn = btnRef.current;
      if (!btn) return;
      const over = e.target === btn || btn.contains(e.target as Node);
      if (over === pointerOnBtn) return;
      syncHitTest(over);
    };
    const onLeave = () => syncHitTest(false);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseleave", onLeave);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseleave", onLeave);
    };
  }, [pointerOnBtn]);

  const onMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    syncHitTest(true);
    dragStartScreen.current = { x: e.screenX, y: e.screenY };
    dragStart(e.screenX, e.screenY);

    const onUp = (ev: MouseEvent) => {
      document.removeEventListener("mouseup", onUp);
      dragStop();
      const start = dragStartScreen.current;
      dragStartScreen.current = null;
      if (!start) return;
      const dx = Math.abs(ev.screenX - start.x);
      const dy = Math.abs(ev.screenY - start.y);
      if (dx < 5 && dy < 5) {
        onExpand();
        return;
      }
      syncHitTest(true);
    };
    document.addEventListener("mouseup", onUp);
  };

  return (
    <div className="collapsed-orb-wrap">
      <button
        ref={btnRef}
        type="button"
        aria-label="Expand Michelle"
        onMouseDown={onMouseDown}
        onMouseEnter={() => syncHitTest(true)}
        onMouseLeave={() => syncHitTest(false)}
        className="collapsed-orb"
      />
    </div>
  );
}
