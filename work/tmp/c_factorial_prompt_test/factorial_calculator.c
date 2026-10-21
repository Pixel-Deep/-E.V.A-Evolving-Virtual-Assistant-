#include <stdio.h>
unsigned long long factorial(int n) { return n < 2 ? 1 : (unsigned long long)n * factorial(n - 1); }
int main(void) {
    printf("Enter a non-negative integer:");
    unsigned long long int num = 0;
    scanf("%llu", &num);
    if (num <= 20) {
        printf("The factorial is: %llu\n", factorial(num));
    } else { puts("Overflow"); }
    return 0;
}
