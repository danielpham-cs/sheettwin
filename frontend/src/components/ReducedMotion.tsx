"use client";

import { MotionConfig } from "motion/react";

/** Honours the OS "reduce motion" setting (WCAG 2.3.3): motion animations then skip movement and scaling. */
export function ReducedMotion({ children }: { children: React.ReactNode }) {
  return <MotionConfig reducedMotion="user">{children}</MotionConfig>;
}
