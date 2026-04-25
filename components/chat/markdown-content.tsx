import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

import { cn } from "@/lib/utils";

interface MarkdownContentProps {
  children: string;
  className?: string;
}

/**
 * Renders chat content as Markdown with math support.
 *
 * Pipeline:
 *   raw text → remark-math (parses $..$, $$..$$) → remark-gfm (tables/lists)
 *            → rehype-katex (turns math into KaTeX HTML)  → React elements
 *
 * Element-level styling lives below in `components`. This is where we keep
 * the chat readable: tight prose, comfortable line-height, no weird default
 * h1 sizes, etc. KaTeX handles math layout itself; we only style around it.
 */
export function MarkdownContent({ children, className }: MarkdownContentProps) {
  return (
    <div className={cn("markdown text-sm leading-relaxed", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkMath, remarkGfm]}
        rehypePlugins={[rehypeKatex]}
        components={markdownComponents}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

/**
 * Per-element renderers. Keep these terse — Tailwind classes only.
 *
 * Goal: feel like prose in a serious math textbook, not a generic blog.
 * Headings stay small (chat context, not a doc), spacing is tight enough
 * that short replies don't look bloated, lists are compact.
 */
const markdownComponents: Components = {
  p: ({ node: _node, ...props }) => (
    <p className="my-2 first:mt-0 last:mb-0" {...props} />
  ),

  // Headings — kept smaller than usual; this is a chat reply, not an article.
  h1: ({ node: _node, ...props }) => (
    <h1
      className="mt-4 mb-2 text-lg font-serif first:mt-0"
      {...props}
    />
  ),
  h2: ({ node: _node, ...props }) => (
    <h2
      className="mt-4 mb-2 text-base font-serif first:mt-0"
      {...props}
    />
  ),
  h3: ({ node: _node, ...props }) => (
    <h3
      className="mt-3 mb-1.5 text-sm font-medium first:mt-0"
      {...props}
    />
  ),

  ul: ({ node: _node, ...props }) => (
    <ul className="my-2 ml-5 list-disc space-y-1 marker:text-muted-foreground" {...props} />
  ),
  ol: ({ node: _node, ...props }) => (
    <ol className="my-2 ml-5 list-decimal space-y-1 marker:text-muted-foreground" {...props} />
  ),
  li: ({ node: _node, ...props }) => (
    <li className="leading-relaxed" {...props} />
  ),

  // Inline + block code. Block code uses our mono font + a subtle surface.
  code: ({ node: _node, className, children, ...props }) => {
    const isBlock = /\blanguage-/.test(className ?? "");
    if (isBlock) {
      return (
        <code
          className={cn(
            "block w-full overflow-x-auto rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs",
            className
          )}
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]"
        {...props}
      >
        {children}
      </code>
    );
  },
  pre: ({ node: _node, ...props }) => (
    <pre className="my-3 first:mt-0 last:mb-0" {...props} />
  ),

  blockquote: ({ node: _node, ...props }) => (
    <blockquote
      className="my-2 border-l-2 border-primary/40 pl-3 text-muted-foreground"
      {...props}
    />
  ),

  hr: () => <hr className="my-4 border-border" />,

  a: ({ node: _node, ...props }) => (
    <a
      className="text-primary underline underline-offset-2 hover:no-underline"
      target="_blank"
      rel="noreferrer noopener"
      {...props}
    />
  ),

  // Tables — small but readable. Useful for math comparisons.
  table: ({ node: _node, ...props }) => (
    <div className="my-3 overflow-x-auto">
      <table
        className="w-full border-collapse text-left text-sm"
        {...props}
      />
    </div>
  ),
  thead: ({ node: _node, ...props }) => (
    <thead className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground" {...props} />
  ),
  th: ({ node: _node, ...props }) => (
    <th className="px-2 py-1.5 font-medium" {...props} />
  ),
  td: ({ node: _node, ...props }) => (
    <td className="border-b border-border/60 px-2 py-1.5 align-top" {...props} />
  ),

  strong: ({ node: _node, ...props }) => (
    <strong className="font-semibold text-foreground" {...props} />
  ),
  em: ({ node: _node, ...props }) => (
    <em className="italic" {...props} />
  )
};
