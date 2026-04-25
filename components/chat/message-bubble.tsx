import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/chat/markdown-content";
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
 * Both render their content as Markdown + KaTeX. Streaming partial input
 * (e.g. half-finished `$x^2 + y^...`) is fine — react-markdown ignores the
 * incomplete math node until it parses; you just see plain text briefly.
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
