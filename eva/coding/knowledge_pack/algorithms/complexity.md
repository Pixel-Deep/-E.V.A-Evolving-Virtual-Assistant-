# Complexity analysis checklist

State input-size variables first; analyze loops, recursion and data structures; distinguish worst/average/best case where relevant; state auxiliary space separately from output storage. Sequential independent loops usually add, nested loops often multiply, but bounds and early exits matter. For recursion, write a recurrence and account for call-stack depth. Validate empirical performance on representative sizes, but do not use timing alone as proof of asymptotic complexity.
