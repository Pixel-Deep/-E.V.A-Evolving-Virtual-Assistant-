#include <stdio.h>
#include <limits.h>
long long calculateFactorial(int num) {
    unsigned long long fact = 1;
    for (int i = 2; i <= num; ++i) {
        if (fact > (unsigned long long)LLONG_MAX / (unsigned long long)i) {
            printf("Overflow occurred!\n");
            return LLONG_MIN;
        }
        fact *= (unsigned long long)i;
    }
    return fact;
}
int main(void) {
    int n;
    printf("Enter number: ");
    if (scanf("%d", &n) != 1) { fprintf(stderr, "Invalid input.\n"); return 1; }
    if (n < 0 || n > 20) { puts("Out of range"); return 1; }
    long long result = calculateFactorial(n);
    if (result == LLONG_MIN) puts("Overflow");
    else printf("The factorial of %d is %lld\n", n, result);
    return 0;
}
