# Merge sort

**Use when:** stable O(n log n) sorting is needed and auxiliary memory is acceptable. Split the sequence into halves, recursively sort each half, then merge two sorted sequences by repeatedly selecting the smaller front item. Preserve stability by selecting the left item when keys compare equal. Typical complexity: O(n log n) time and O(n) auxiliary memory.

**Edge tests:** empty/singleton input, already sorted, reverse sorted, duplicate keys, negative values, and stability for equal keys. Prefer the language's standard sort unless educational or specific stability/complexity requirements justify a custom implementation.
