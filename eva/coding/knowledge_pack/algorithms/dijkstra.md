# Dijkstra shortest paths

Use only when all edge weights are non-negative. With an adjacency list and min-priority queue, repeatedly pop the smallest tentative distance, skip stale heap entries, and relax outgoing edges. Typical complexity O((V+E) log V). Use a different algorithm when negative edges are allowed. Distances to unreachable vertices remain infinity/null.

**Edge tests:** source equals destination, disconnected vertices, zero-weight edges, parallel edges and large path sums. Guard integer overflow in fixed-width languages. Never claim correctness with negative-weight edges.
