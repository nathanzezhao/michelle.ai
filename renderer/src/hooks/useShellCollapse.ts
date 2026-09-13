import { useCallback, useRef, useState } from "react";
import { setCollapsed, setIgnoreMouse } from "@/ipc/electron";
import type { UiMode } from "@/types/ui";

const COLLAPSED_HEIGHT = 56;
const FADE_OUT_MS = 200;
const FADE_IN_MS = 300;

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function animateShellHeight(shell: HTMLElement, toPx: number): Promise<void> {
  return new Promise((resolve) => {
    const onEnd = (event: TransitionEvent) => {
      if (event.propertyName !== "height") return;
      shell.removeEventListener("transitionend", onEnd);
      resolve();
    };
    shell.addEventListener("transitionend", onEnd);
    shell.style.height = `${toPx}px`;
  });
}

export function useShellCollapse(mode: UiMode, setMode: (next: UiMode) => void) {
  const shellRef = useRef<HTMLDivElement>(null);
  const savedHeightRef = useRef(600);
  const preCollapseModeRef = useRef<UiMode>("chat");
  const [contentOpacity, setContentOpacity] = useState(1);
  const [collapseBtnCircle, setCollapseBtnCircle] = useState(false);
  const [isAnimating, setIsAnimating] = useState(false);
  const collapseGuardRef = useRef<(() => Promise<void> | void) | null>(null);

  const registerCollapseGuard = useCallback((guard: (() => Promise<void> | void) | null) => {
    collapseGuardRef.current = guard;
  }, []);

  const beginCollapse = useCallback(async () => {
    if (mode === "collapsed" || isAnimating) return;
    const shell = shellRef.current;
    if (!shell) return;

    setIsAnimating(true);
    preCollapseModeRef.current = mode;
    savedHeightRef.current = shell.clientHeight;

    if (collapseGuardRef.current) {
      await collapseGuardRef.current();
    }

    setCollapseBtnCircle(true);
    setContentOpacity(0);
    await wait(FADE_OUT_MS);

    shell.style.overflow = "hidden";
    shell.style.height = `${shell.clientHeight}px`;
    shell.offsetHeight;
    await animateShellHeight(shell, COLLAPSED_HEIGHT);

    setCollapsed(true);
    setIgnoreMouse(false);
    setMode("collapsed");
    setIsAnimating(false);
  }, [isAnimating, mode, setMode]);

  const beginExpand = useCallback(async () => {
    if (isAnimating) return;
    const shell = shellRef.current;
    if (!shell) return;

    setIsAnimating(true);
    setCollapsed(false);
    setIgnoreMouse(false);
    setCollapseBtnCircle(false);

    const restoreMode = preCollapseModeRef.current;
    setMode(restoreMode);
    setContentOpacity(0);

    shell.style.height = `${COLLAPSED_HEIGHT}px`;
    shell.style.overflow = "hidden";
    shell.offsetHeight;

    const heightDone = animateShellHeight(shell, savedHeightRef.current);
    await wait(FADE_OUT_MS);
    setContentOpacity(1);
    await heightDone;

    shell.style.removeProperty("height");
    shell.style.removeProperty("overflow");
    setIsAnimating(false);
  }, [isAnimating, setMode]);

  const crossfadeTo = useCallback(
    async (next: UiMode) => {
      if (isAnimating || mode === next || mode === "collapsed") return;
      if (next !== "chat" && next !== "voice") return;

      setIsAnimating(true);
      if (mode === "voice" && collapseGuardRef.current) {
        await collapseGuardRef.current();
      }

      setContentOpacity(0);
      await wait(FADE_OUT_MS);
      setMode(next);
      await wait(16);
      setContentOpacity(1);
      await wait(FADE_IN_MS);
      setIsAnimating(false);
    },
    [isAnimating, mode, setMode]
  );

  return {
    shellRef,
    contentOpacity,
    collapseBtnCircle,
    isAnimating,
    beginCollapse,
    beginExpand,
    crossfadeTo,
    registerCollapseGuard,
  };
}
