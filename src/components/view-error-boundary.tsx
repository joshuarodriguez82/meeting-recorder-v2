"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

interface Props {
  children: ReactNode;
  // Shown in the fallback heading ("The Sessions view hit a problem")
  // so the message reads as "this one tab broke", not "the app broke".
  viewName: string;
}

interface State {
  error: Error | null;
}

/**
 * Per-view crash containment. React only offers error boundaries as
 * class components (there's no hook equivalent, and base-ui/shadcn don't
 * ship one) — this is the standard, dependency-free implementation.
 *
 * Wraps each view in src/app/page.tsx so a render-time throw (e.g. a
 * partial/malformed API payload that slipped past a guard) unmounts
 * only that view's subtree instead of the whole window. The sidebar,
 * nav, and every other tab stay alive and clickable — this is
 * specifically the failure mode GOAL 1 exists to prevent: a bad
 * response from one endpoint white-screening the entire app instead of
 * degrading just the view that needed it.
 *
 * Retry re-attempts the render with the same props; since this class
 * has no key tied to `nav`, switching tabs away and back also gets a
 * clean remount for free (the parent's conditional `nav === "x" &&`
 * unmounts this whole boundary along with its children).
 */
export class ViewErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.viewName} view] crashed:`, error, info.componentStack);
  }

  private retry = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="mx-auto mt-10 max-w-xl space-y-3 rounded-2xl border border-destructive/25 bg-destructive/5 p-6 text-center">
        <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <AlertTriangle className="h-5 w-5" />
        </div>
        <h2 className="text-lg font-semibold text-foreground">
          The {this.props.viewName} view hit a problem
        </h2>
        <p className="text-sm text-muted-foreground">
          Something failed while rendering this view — often a partial or
          unexpected response from the backend. The rest of the app is
          unaffected; switching tabs and coming back also resets this view.
        </p>
        <p className="rounded-md bg-muted/60 px-3 py-2 text-left font-mono text-[11px] text-muted-foreground break-all">
          {error.message || String(error)}
        </p>
        <button
          type="button"
          onClick={this.retry}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3.5 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          Retry
        </button>
      </div>
    );
  }
}
