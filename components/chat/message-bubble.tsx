import { CheckCircle2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/chat/markdown-content";
import type { Message } from "@/lib/api/types";

interface MessageBubbleProps {
  message: Pick<Message, "role" | "content">;
  authorLabel: string;
  isStreaming?: boolean;
  /**
   * Phase 10B: when true, render a small "guided mode" check-icon
   * + tooltip alongside the assistant's author label. Decision M:
   * NO progress bar, NO step counter visible to the student -- the
   * icon just signals "this is a verified problem with a structured
   * path", justifying why the tutor is suddenly more precise.
   */
  guidedModeBadge?: {
    label: string;
    tooltip: string;
  };
}

/**
 * Single message renderer.
 *
 * Claude-style:
 *  - User messages: right-aligned, soft primary-tinted card
 *  - Assistant messages: left-aligned, plain prose with a small label
 *
 * Both render their content as Markdown + KaTeX. Streaming partial input
 * (e.g. half-finished `$x^2 + y^...`) is fine — react-markdown ignores the
 * incomplete math node until it parses; you just see plain text briefly.
 */
export function MessageBubble({
  message,
  authorLabel,
  isStreaming,
  guidedModeBadge,
}: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start"
      )}
    >
      <div className={cn("flex max-w-[85%] flex-col gap-1.5 sm:max-w-[78%]")}>
        <div
          className={cn(
            "flex items-center gap-1.5 text-xs font-medium text-muted-foreground",
            isUser ? "justify-end" : "justify-start"
          )}
        >
          <span>{authorLabel}</span>
          {!isUser && guidedModeBadge && (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-muted/60 px-1.5 py-0.5 text-[10px] font-normal"
              title={guidedModeBadge.tooltip}
              aria-label={guidedModeBadge.tooltip}
            >
              <CheckCircle2 className="h-3 w-3" aria-hidden />
              {guidedModeBadge.label}
            </span>
          )}
        </div>
        <div
          className={cn(
            "rounded-2xl px-4 py-3 text-foreground break-words",
            isUser
              ? "bg-primary/10"
              : "bg-transparent"
          )}
        >
          <MarkdownContent>{message.content}</MarkdownContent>
          {isStreaming && (
            <span
              aria-hidden
              className="ml-0.5 inline-block h-4 w-[2px] translate-y-[2px] animate-pulse bg-foreground/60"
            />
          )}
        </div>
      </div>
    </div>
  );
}
