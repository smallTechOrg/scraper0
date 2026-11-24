"use client";

import { Suspense } from "react";
import { LoadingState } from "@/components/ui";
import { NewJob } from "./new-job";

export default function NewJobPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <NewJob />
    </Suspense>
  );
}
