import { cn } from "@/lib/utils";
import type { Message } from "@/lib/api/types";

interface MessageBubbleProps {
  message: Pick<Message, "role" | "content">;
  authorLabel: string;
  isStreaming?: boolean;
}

/**
 * Single message renderer.
 *
 * Claude-style:
 *  - User messages: right-aligned, soft primary-tinted card
 *  - Assistant messages: left-aligned, plain prose with a small label
 *
 * Markdown / KaTeX rendering is intentionally not added yet — content is
 * rendered as plain text with preserved newlines. We'll layer on
 * react-markdown + KaTeX in a focused pass once the chat is stable.
 */
export function MessageBubble({
  message,
  authorLabel,
  isStreaming
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
        <span
          className={cn(
            "text-xs font-medium text-muted-foreground",
            isUser ? "text-right" : "text-left"
          )}
        >
          {authorLabel}
        </span>
        <div
          className={cn(
            "rounded-2xl px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap break-words",
            isUser
              ? "bg-primary/10 text-foreground"
              : "bg-transparent text-foreground"
          )}
        >
          {message.content}
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
