/**
 * Calculate the fibonacci sequence up to n terms using memoization.
 * Returns an array of the first n fibonacci numbers.
 */
export function fibonacci(n: number): number[] {
  const memo = new Map<number, number>();
  function fib(k: number): number {
    if (k <= 1) return k;
    if (memo.has(k)) return memo.get(k)!;
    const result = fib(k - 1) + fib(k - 2);
    memo.set(k, result);
    return result;
  }
  return Array.from({ length: n }, (_, i) => fib(i));
}
