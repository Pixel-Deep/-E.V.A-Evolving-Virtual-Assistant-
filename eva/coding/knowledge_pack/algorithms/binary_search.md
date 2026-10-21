# Binary search

**Use when:** data is sorted and random access is available. **Precondition:** input ordering is ascending (or the comparison is adapted consistently). **Complexity:** O(log n) time, O(1) extra space for iterative form.

Maintain a half-open interval `[lo, hi)`. While `lo < hi`, compute `mid = lo + (hi - lo) // 2`. If `items[mid] < target`, set `lo = mid + 1`; otherwise set `hi = mid`. At termination, `lo` is the lower-bound insertion point. For exact search, confirm `lo < len(items)` and `items[lo] == target`.

**Edge tests:** empty list, one element, absent target below/above range, duplicates, first and last element. **Common mistake:** using this on unsorted data or mixing inclusive and half-open bounds.
