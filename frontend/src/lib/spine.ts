/**
 * Pure helper that exists to prove the TypeScript + Vite + Vitest spine end to
 * end (longterm_todo.md item #8, Phase 0). No dependencies, no side effects, so
 * it is safe to unit-test in isolation — the template every migrated module
 * follows: pure inputs, pure outputs, co-located test.
 */
export function spineGreeting(name: string): string {
  return `vite/ts spine alive for ${name}`;
}
