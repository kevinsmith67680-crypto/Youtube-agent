"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

/** A focusable trigger with a tooltip. The description is also in the accessible name
 * of the trigger (aria-describedby is set by Radix on open; the sr-only copy below keeps
 * it available to screen readers without hovering). */
export function InfoTip({
  children,
  content,
  className = "",
}: {
  children: ReactNode;
  content: string;
  className?: string;
}) {
  return (
    <Tooltip.Root delayDuration={150}>
      <Tooltip.Trigger asChild>
        <button type="button" className={`cursor-help ${className}`}>
          {children}
          <span className="sr-only">: {content}</span>
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side="top"
          sideOffset={6}
          className="z-50 max-w-xs rounded-md border border-line bg-raised px-3 py-2 text-xs leading-snug text-ink shadow-lg"
        >
          {content}
          <Tooltip.Arrow className="fill-raised" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export function TooltipProvider({ children }: { children: ReactNode }) {
  return <Tooltip.Provider>{children}</Tooltip.Provider>;
}
