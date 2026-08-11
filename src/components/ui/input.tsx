import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(
        // Resting state carries a visible `border-input` hairline — the
        // same token Select/Textarea already use — so a pill input reads
        // as editable rather than as a disabled chip. Design review
        // 2026-08-11: the empty "Meeting Name" field on the Record page
        // was `border-transparent` over a flat `bg-muted` fill, which
        // looked inert next to the bordered Template/Client controls and
        // users assumed it wasn't typeable. The fill is softened to
        // muted/70 and lifts to full muted on hover so the control feels
        // live; disabled deliberately DROPS the border and keeps the
        // dimmed fill + opacity, so "disabled" and "resting" can't be
        // confused in either direction.
        "h-8 w-full min-w-0 rounded-full border border-input bg-muted/70 px-3.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground hover:bg-muted focus-visible:border-ring focus-visible:bg-card focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:border-transparent disabled:bg-muted/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className
      )}
      {...props}
    />
  )
}

export { Input }
