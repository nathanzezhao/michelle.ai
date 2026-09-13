type IpcRenderer = {
  invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  send(channel: string, ...args: unknown[]): void;
  sendSync(channel: string, ...args: unknown[]): unknown;
  on(channel: string, listener: (...args: unknown[]) => void): void;
  removeListener(channel: string, listener: (...args: unknown[]) => void): void;
};

function ipc(): IpcRenderer {
  const req = (window as unknown as { require?: (id: string) => unknown }).require;
  if (!req) {
    throw new Error("Electron ipcRenderer unavailable");
  }
  return req("electron").ipcRenderer as IpcRenderer;
}

export async function micStart(): Promise<{ ok: boolean; message?: string; name?: string }> {
  const out = (await ipc().invoke("mic-start")) as { ok: boolean; message?: string; name?: string };
  return out ?? { ok: false, message: "mic start failed" };
}

export async function micStop(): Promise<Blob | null> {
  const buf = (await ipc().invoke("mic-stop")) as ArrayBuffer | Buffer | null;
  if (!buf) return null;
  const bytes = buf instanceof ArrayBuffer ? buf : Uint8Array.from(buf as Buffer);
  if (!bytes.byteLength) return null;
  return new Blob([bytes], { type: "audio/wav" });
}

export function micStatus(): { granted: boolean; status: string } {
  try {
    return (ipc().sendSync("microphone-status") as { granted: boolean; status: string }) ?? {
      granted: false,
      status: "unknown",
    };
  } catch {
    return { granted: false, status: "unknown" };
  }
}

export async function askMicrophone(): Promise<{ granted: boolean; prompted: boolean; status: string }> {
  return (await ipc().invoke("ask-microphone")) as {
    granted: boolean;
    prompted: boolean;
    status: string;
  };
}

export function setCollapsed(collapsed: boolean): void {
  ipc().sendSync("collapse", collapsed);
}

export function setIgnoreMouse(ignore: boolean): void {
  ipc().sendSync("set-ignore-mouse", ignore);
}

export function dragStart(screenX: number, screenY: number): void {
  ipc().send("drag-start", { screenX, screenY });
}

export function dragStop(): void {
  ipc().send("drag-stop");
}

export function setUiMode(mode: string): void {
  ipc().send("ui-mode", mode);
}
