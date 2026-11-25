// Tiny static server for tests. Serves site/ on 127.0.0.1:4173 and one generated variant,
// /override.html, which proves the API base can be overridden for tests (it loosens only connect-src).
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json' };

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  try {
    if (url.pathname === '/override.html') {
      let html = await readFile(join(root, 'index.html'), 'utf8');
      html = html.replace('connect-src https://api.1507.systems', "connect-src 'self'");
      html = html.replace('<meta name="viewport"', '<meta name="provenage-base" content="http://127.0.0.1:4173/fake-api">\n<meta name="viewport"');
      res.writeHead(200, { 'content-type': types['.html'] }).end(html);
      return;
    }
    if (url.pathname === '/fake-api/calendar/index.json') {
      const body = await readFile(join(root, 'test/fixtures/index.json'));
      res.writeHead(200, { 'content-type': types['.json'] }).end(body);
      return;
    }
    const rel = normalize(url.pathname === '/' ? '/index.html' : url.pathname);
    if (rel.includes('..') || rel.startsWith('/test/') || rel.startsWith('/node_modules/')) throw new Error('not served');
    const body = await readFile(join(root, rel));
    const ext = rel.slice(rel.lastIndexOf('.'));
    res.writeHead(200, { 'content-type': types[ext] ?? 'application/octet-stream' }).end(body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
  }
});
server.listen(4173, '127.0.0.1');
