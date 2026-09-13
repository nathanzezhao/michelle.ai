import Orb from "@/components/Orb/Orb";
import type { VoiceRecordingState } from "@/types/ui";

type AiBlobProps = {
  state: VoiceRecordingState;
  onClick: () => void;
  statusText?: string;
};

/** Match panel tint so the WebGL orb blends with the shell, not a dark void. */
const VOICE_PANEL_BG = "rgb(241, 241, 250)";

const orbPropsForState = (state: VoiceRecordingState) => {
  if (state === "recording") {
    return { forceHoverState: true, hoverIntensity: 0.9, rotateOnHover: true, hue: 245 };
  }
  if (state === "processing") {
    return { forceHoverState: true, hoverIntensity: 0.55, rotateOnHover: true, hue: 210 };
  }
  return { forceHoverState: false, hoverIntensity: 0.3, rotateOnHover: true, hue: 230 };
};

/** Voice orb — React Bits free `Orb` (WebGL). Pro-only `ai-blob-tw` not required. */
export function AiBlob({ state, onClick, statusText }: AiBlobProps) {
  const orbProps = orbPropsForState(state);

  return (
    <button
      type="button"
      data-state={state}
      aria-label={
        state === "recording" ? "Stop recording" : state === "processing" ? "Processing" : "Start recording"
      }
      onClick={onClick}
      disabled={state === "processing"}
      className="voice-blob relative flex flex-col items-center justify-center border-none bg-transparent p-0 cursor-pointer disabled:cursor-wait"
    >
      <div className="absolute inset-0 overflow-hidden rounded-full">
        <Orb {...orbProps} backgroundColor={VOICE_PANEL_BG} />
      </div>
      {statusText ? (
        <span className="absolute -bottom-8 text-[11px] text-[#333] opacity-80 whitespace-nowrap">
          {statusText}
        </span>
      ) : null}
    </button>
  );
}
