// Minimal typings for the few Node APIs the fixture-driven tests use, so the Worker project does not need
// @types/node (which would clash with @cloudflare/workers-types globals). Tests only; never bundled.
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string;
  export function readdirSync(path: string): string[];
  export function statSync(path: string): { isDirectory(): boolean };
  export function mkdirSync(path: string, options?: { recursive?: boolean }): void;
  export function writeFileSync(path: string, data: string, encoding: 'utf8'): void;
}
declare module 'node:crypto' {
  export function createHash(alg: 'sha256'): { update(data: string, enc: 'utf8'): { digest(enc: 'hex'): string } };
}
declare module 'node:path' {
  export function join(...parts: string[]): string;
  export function dirname(path: string): string;
}
declare module 'node:url' {
  export function fileURLToPath(url: string): string;
}
declare const process: { env: Record<string, string | undefined> };
declare const Buffer: {
  from(s: string, enc: 'utf8'): Uint8Array;
  byteLength(s: string): number;
};
interface ImportMeta {
  url: string;
}
