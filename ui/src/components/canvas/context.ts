import { createContext, useContext } from "react";

import { EMPTY_LOOKUP, type CanvasContextValue } from "./types";

export const CanvasContext = createContext<CanvasContextValue>({
  nodeIssues: new Map(),
  edgeIssues: new Map(),
  lookup: EMPTY_LOOKUP,
  connectionHint: () => null,
  proposalMarks: new Map(),
});

export function useCanvasContext(): CanvasContextValue {
  return useContext(CanvasContext);
}
