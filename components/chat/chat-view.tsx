"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowLeft, MessageSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusMessage } from "@/components/ui/status-message";
import { Logo } from "@/components/brand/logo";
import { ChatInput, type ChatInputHandle } from "@/components/chat/chat-input";
import { MessageBubble } from "@/components/chat/message-bubble";
import { Link } from "@/i18n/navigation";
import { ApiError } from "@/lib/api/config";
import { streamChat } from "@/lib/api/chat";
import { getSession } from "@/lib/api/sessions";
import type { Message, TutorSession } from "@/lib/api/types";
import { cn } from "@/lib/utils";

interface ChatViewProps {
  sessionId: string;
}

/**
 * Full chat experience for one tutor session.
 *
 * Lifecycle:
 *   1. Mount → load session + existing messages.
 *   2. User sends a message → optimistically render their bubble + an empty
 *      assistant bubble that fills in token-by-token via SSE.
 *   3. On `done`, the assistant message is "sealed" — it's been persisted
 *      server-side too. On `error`, we show a status message and let the
 *      user retry.
 *   4. On `title` (first turn only), the local session title updates.
 */
export function ChatView({ sessionId }: ChatViewProps) {
  const t = useTranslations("chat");
  const tSessions = useTranslations("sessions");

  const [session, setSession] = useState<TutorSession | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadStatus, setLoadStatus] = useState<
    "loading" | "ready" | "not_found" | "error"
  >("loading");
  const [loadError, setLoadError] = useState<string | null>(null);

  const [input, setInput] = useState("");
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<ChatInputHandle | null>(null);

  // Load session + messages on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await getSession(sessionId);
        if (cancelled) return;
        setSession(data.session);
        setMessages(data.messages);
        setLoadStatus("ready");
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setLoadStatus("not_found");
        } else {
          setLoadError(e instanceof ApiError ? e.detail : t("loadError"));
          setLoadStatus("error");
        }
      }
    })();
    return () => {
      cancelled = true;
      abortRef.current?.abort();
    };
  }, [sessionId, t]);

  // Auto-scroll to bottom whenever messages change or streaming content updates.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, streamingContent]);

  async function handleSend() {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;

    setStreamError(null);
    setInput("");
    setIsStreaming(true);
    setStreamingContent("");

    // Optimistic: render the user's message immediately.
    const optimisticUserMessage: Message = {
      id: `optimistic-${Date.now()}`,
      session_id: sessionId,
      role: "user",
      content: trimmed,
      created_at: new Date().toISOString()
    };
    setMessages((prev) => [...prev, optimisticUserMessage]);

    const controller = new AbortController();
    abortRef.current = controller;

    let accumulated = "";

    try {
      await streamChat({
        sessionId,
        message: trimmed,
        signal: controller.signal,
        onToken: (token) => {
          accumulated += token;
          setStreamingContent(accumulated);
        },
        onTitle: (title) => {
          setSession((prev) => (prev ? { ...prev, title } : prev));
        },
        onError: (msg) => {
          setStreamError(msg);
        },
        onDone: () => {
          // handled below in the finally block
        }
      });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        // user-initiated stop — keep whatever we have so far
      } else {
        setStreamError(e instanceof ApiError ? e.detail : t("streamError"));
      }
    } finally {
      // Convert the streamed content into a sealed assistant message.
      if (accumulated.trim()) {
        const assistantMessage: Message = {
          id: `optimistic-assistant-${Date.now()}`,
          session_id: sessionId,
          role: "assistant",
          content: accumulated,
          created_at: new Date().toISOString()
        };
        setMessages((prev) => [...prev, assistantMessage]);
      }
      setStreamingContent(null);
      setIsStreaming(false);
      abortRef.current = null;
      // Re-focus the input for fast follow-ups.
      inputRef.current?.focus();
    }
  }

  function handleStop() {
    abortRef.current?.abort();
  }

  if (loadStatus === "loading") {
    return (
      <div className="flex h-screen items-center justify-center">
        <Logo size="md" className="animate-pulse opacity-40" />
      </div>
    );
  }

  if (loadStatus === "not_found") {
    return <CenteredFallback message={t("notFound")} />;
  }

  if (loadStatus === "error") {
    return <CenteredFallback message={loadError ?? t("loadError")} />;
  }

  const title = session?.title?.trim() || tSessions("untitled");
  const hasContent = messages.length > 0 || streamingContent !== null;

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-border bg-background/95 px-4 py-3 backdrop-blur sm:px-6">
        <Button
          asChild
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("back")}
        >
          <Link href="/dashboard/sessions">
            <ArrowLeft className="h-5 w-5" />
          </Link>
        </Button>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <MessageSquare
            className="h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden
          />
          <h1 className="truncate text-base font-medium text-foreground">
            {title}
          </h1>
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-8 sm:px-6">
          {!hasContent ? (
            <EmptyChat />
          ) : (
            <>
              {messages.map((m) => (
                <MessageBubble
                  key={m.id}
                  message={m}
                  authorLabel={m.role === "user" ? t("you") : t("tutor")}
                />
              ))}
              {streamingContent !== null && (
                <MessageBubble
                  message={{ role: "assistant", content: streamingContent }}
                  authorLabel={t("tutor")}
                  isStreaming
                />
              )}
            </>
          )}

          {streamError && (
            <StatusMessage type="error">{streamError}</StatusMessage>
          )}
        </div>
      </div>

      <ChatInput
        ref={inputRef}
        value={input}
        onChange={setInput}
        onSubmit={handleSend}
        onStop={handleStop}
        placeholder={t("placeholder")}
        sendLabel={t("send")}
        stopLabel={t("stop")}
        isStreaming={isStreaming}
        autoFocus
      />
    </div>
  );
}

function EmptyChat() {
  const t = useTranslations("chat");
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <h2 className="text-2xl font-serif text-foreground">
        {t("emptyTitle")}
      </h2>
      <p className="max-w-md text-sm text-muted-foreground leading-relaxed">
        {t("emptyDescription")}
      </p>
    </div>
  );
}

function CenteredFallback({ message }: { message: string }) {
  const t = useTranslations("chat");
  return (
    <div
      className={cn(
        "flex h-screen flex-col items-center justify-center gap-4 px-6 text-center"
      )}
    >
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button asChild type="button" variant="outline">
        <Link href="/dashboard/sessions">
          <ArrowLeft className="h-4 w-4" />
          {t("back")}
        </Link>
      </Button>
    </div>
  );
}
