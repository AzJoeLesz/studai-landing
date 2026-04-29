"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowLeft, MessageSquare, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusMessage } from "@/components/ui/status-message";
import { Logo } from "@/components/brand/logo";
import { ChatInput, type ChatInputHandle } from "@/components/chat/chat-input";
import { MessageBubble } from "@/components/chat/message-bubble";
import { Link } from "@/i18n/navigation";
import { ApiError } from "@/lib/api/config";
import { streamChat, type GuidedActivePayload } from "@/lib/api/chat";
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
 *      server-side too. On `error`, we surface a Retry button (see below).
 *   4. On `title` (first turn only), the local session title updates.
 *
 * Retry-after-connection-drop:
 *   When the SSE stream errors mid-flight (most commonly because Railway's
 *   reverse proxy killed an idle TCP connection while gpt-5-mini was
 *   thinking), we keep the user message visible and show a Retry button.
 *   On Retry we first re-fetch the session — if the assistant reply landed
 *   server-side and we just lost it on the wire, we display it without
 *   triggering another LLM call. Otherwise we re-send the same message.
 *   The backend (see `agents/tutor.py`) is idempotent on duplicate user
 *   messages so a retry is safe.
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
  // The text of the most recent user message we tried to send. Held so the
  // Retry button knows what to re-send. Cleared after a clean turn.
  const [pendingRetryMessage, setPendingRetryMessage] = useState<string | null>(
    null,
  );
  // Phase 10B: when guided mode is active for the currently-streaming
  // turn, the backend emits a `guided_active` SSE event before any
  // tokens. We render a small check-icon badge on the streaming bubble,
  // and once the assistant message is sealed we record its id in
  // `guidedMessageIds` so the badge persists after scroll.
  const [streamingGuidedActive, setStreamingGuidedActive] =
    useState<GuidedActivePayload | null>(null);
  const [guidedMessageIds, setGuidedMessageIds] = useState<Set<string>>(
    () => new Set(),
  );

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

  /**
   * Run the streaming exchange for one user message. Used by both the
   * initial send and by the Retry button. `addOptimisticUserBubble`
   * controls whether we render the "You: ..." bubble — on retry we
   * already have it on screen, so we don't add another.
   */
  async function startStream(
    userMessage: string,
    { addOptimisticUserBubble }: { addOptimisticUserBubble: boolean },
  ) {
    setStreamError(null);
    setIsStreaming(true);
    setStreamingContent("");
    setStreamingGuidedActive(null);
    setPendingRetryMessage(userMessage);

    if (addOptimisticUserBubble) {
      const optimisticUserMessage: Message = {
        id: `optimistic-${Date.now()}`,
        session_id: sessionId,
        role: "user",
        content: userMessage,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimisticUserMessage]);
    }

    const controller = new AbortController();
    abortRef.current = controller;

    let accumulated = "";
    let cleanFinish = false;
    let guidedForThisTurn: GuidedActivePayload | null = null;

    try {
      await streamChat({
        sessionId,
        message: userMessage,
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
          cleanFinish = true;
        },
        onGuidedActive: (payload) => {
          guidedForThisTurn = payload;
          setStreamingGuidedActive(payload);
        },
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
        const assistantId = `optimistic-assistant-${Date.now()}`;
        const assistantMessage: Message = {
          id: assistantId,
          session_id: sessionId,
          role: "assistant",
          content: accumulated,
          created_at: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, assistantMessage]);
        if (guidedForThisTurn) {
          setGuidedMessageIds((prev) => {
            const next = new Set(prev);
            next.add(assistantId);
            return next;
          });
        }
      }
      setStreamingContent(null);
      setStreamingGuidedActive(null);
      setIsStreaming(false);
      abortRef.current = null;
      // Only clear the retry handle on a clean done. Mid-stream errors
      // and aborts both leave it set so the Retry button can use it.
      if (cleanFinish && !accumulated.trim()) {
        // Edge case: clean `done` with zero tokens. Let the user retry
        // because the result is unusable.
      } else if (cleanFinish) {
        setPendingRetryMessage(null);
      }
      // Re-focus the input for fast follow-ups.
      inputRef.current?.focus();
    }
  }

  async function handleSend() {
    const trimmed = input.trim();
    if (!trimmed || isStreaming) return;
    setInput("");
    await startStream(trimmed, { addOptimisticUserBubble: true });
  }

  /**
   * Retry the last failed user message.
   *
   * Step 1: re-fetch the session. If the assistant reply was actually
   * persisted before the wire died, just refresh the UI — no new LLM call.
   * Step 2: otherwise, re-send the same message. The backend de-dups
   * the user-message persistence so this is safe to call repeatedly.
   */
  async function handleRetry() {
    if (!pendingRetryMessage || isStreaming) return;
    setStreamError(null);

    let messagesAfterReload: Message[] | null = null;
    try {
      const data = await getSession(sessionId);
      messagesAfterReload = data.messages;
      setMessages(data.messages);
    } catch {
      // If reload fails, fall through and try the stream anyway.
    }

    if (
      messagesAfterReload &&
      messagesAfterReload.length > 0 &&
      messagesAfterReload[messagesAfterReload.length - 1].role === "assistant"
    ) {
      // The reply did land server-side; we just lost it on the wire.
      // Treat as success.
      setPendingRetryMessage(null);
      return;
    }

    await startStream(pendingRetryMessage, {
      addOptimisticUserBubble: false,
    });
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

  // Phase 10B: build the i18n'd badge object once per render so we
  // don't allocate inside the messages.map.
  const guidedBadge = {
    label: t("guidedMode"),
    tooltip: t("guidedModeTooltip"),
  };

  // Show the Retry button when:
  //   - we have a pending message we tried to send,
  //   - we're not currently in the middle of streaming,
  //   - and there's an error to recover from.
  const canRetry =
    !!pendingRetryMessage && !isStreaming && !!streamError;

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
                  guidedModeBadge={
                    m.role === "assistant" && guidedMessageIds.has(m.id)
                      ? guidedBadge
                      : undefined
                  }
                />
              ))}
              {streamingContent !== null && (
                <MessageBubble
                  message={{ role: "assistant", content: streamingContent }}
                  authorLabel={t("tutor")}
                  isStreaming
                  guidedModeBadge={
                    streamingGuidedActive ? guidedBadge : undefined
                  }
                />
              )}
            </>
          )}

          {streamError && (
            <div className="flex flex-col items-start gap-2">
              <StatusMessage type="error">{streamError}</StatusMessage>
              {canRetry && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleRetry}
                >
                  <RotateCcw className="h-4 w-4" />
                  {t("retry")}
                </Button>
              )}
            </div>
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
