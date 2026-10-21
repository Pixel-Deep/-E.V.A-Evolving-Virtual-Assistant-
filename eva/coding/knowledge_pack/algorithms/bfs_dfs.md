# Graph traversal: BFS and DFS

**BFS:** use a queue; mark a vertex visited when enqueuing it to prevent duplicate queue entries. Finds shortest path length in unweighted graphs. Complexity O(V+E) with adjacency lists.

**DFS:** use recursion or an explicit stack; mark visited before exploring neighbours. Useful for reachability, components, cycle detection variants and traversal ordering. Complexity O(V+E).

**Edge tests:** disconnected graph, isolated vertex, self-loop, cycle, duplicate edge and empty graph. State whether the graph is directed and whether vertices may be missing from the adjacency map.
