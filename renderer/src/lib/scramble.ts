export function revealScramble(
  element: HTMLElement,
  finalString: string
): ReturnType<typeof setInterval> {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  let iterations = 0;
  const interval = window.setInterval(() => {
    let scrambledText = "";
    for (let i = 0; i < finalString.length; i += 1) {
      if (i < iterations) scrambledText += finalString[i];
      else if (finalString[i] === " ") scrambledText += " ";
      else scrambledText += chars[Math.floor(Math.random() * chars.length)];
    }
    element.innerText = scrambledText;
    if (iterations >= finalString.length) clearInterval(interval);
    iterations += 0.5;
  }, 10);
  return interval;
}
