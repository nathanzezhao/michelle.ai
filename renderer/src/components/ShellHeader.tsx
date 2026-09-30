import type { ReactNode } from "react";
import { useDragOrClick } from "@/hooks/useDragOrClick";

type ShellHeaderProps = {
  collapseCircle: boolean;
  onCollapse: () => void;
  trailing?: ReactNode;
  hidden?: boolean;
};

export function ShellHeader({ collapseCircle, onCollapse, trailing, hidden }: ShellHeaderProps) {
  const pointer = useDragOrClick(onCollapse);

  return (
    <header
      className={`shell-header relative shrink-0 ${hidden ? "shell-header--hidden" : ""}`}
      style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
    >
      <button
        type="button"
        id="collapse-btn"
        aria-label="Collapse (drag to move)"
        onPointerDown={pointer.onPointerDown}
        onPointerUp={pointer.onPointerUp}
        onClick={pointer.onClick}
        className={`shell-collapse-btn ${collapseCircle ? "shell-collapse-btn--circle" : ""}`}
        style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}
      />
      <span className="shell-title">chelle</span>
      {trailing ? (
        <div className="shell-trailing" style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}>
          {trailing}
        </div>
      ) : null}
    </header>
  );
}
