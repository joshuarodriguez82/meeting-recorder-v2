"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      // Typographic scale (design review 2026-08-14): field labels at
      // 13px medium — small enough to read as "label" rather than
      // "heading", distinct from the 12px help text underneath them and
      // the 18px card headings above.
      className={cn(
        "flex items-center gap-2 text-[13px] leading-none font-medium select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50 peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Label }
