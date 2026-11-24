/// <reference lib="webworker" />
// Runs computeDeterministicLayout off the main thread so large graphs never freeze the page.
import { computeDeterministicLayout } from "./physics";
import type { LayoutRequest, LayoutResponse } from "./useGraphLayout";

self.onmessage = (event: MessageEvent<LayoutRequest>) => {
  const { id, nodes, edges, clusters, options } = event.data;
  try {
    const result = computeDeterministicLayout(nodes, edges, clusters, options);
    const reply: LayoutResponse = { id, result };
    self.postMessage(reply);
  } catch (err) {
    const reply: LayoutResponse = { id, error: String(err) };
    self.postMessage(reply);
  }
};
