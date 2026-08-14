"use client"

import type { ReactNode } from "react"
import { Info } from "lucide-react"

import { cn } from "@/lib/utils"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"

/**
 * Small "?" affordance that reveals a long explanation on click, instead
 * of the paragraph sitting inline permanently. Used to shrink the
 * grey-text footprint on Record/Settings (design review 2026-08-14)
 * without deleting any of the underlying explanation — every paragraph
 * moved behind one of these stays reachable in full, just click-to-open
 * rather than always-on.
 */
function InfoTip({
  children,
  className,
  label = "More info",
}: {
  children: ReactNode
  className?: string
  label?: string
}) {
  return (
    <Popover>
      <PopoverTrigger
        aria-label={label}
        className={cn(
          "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-muted-foreground/70 outline-none transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50",
          className
        )}
      >
        <Info className="h-3.5 w-3.5" />
      </PopoverTrigger>
      <PopoverContent>{children}</PopoverContent>
    </Popover>
  )
}

export { InfoTip }
