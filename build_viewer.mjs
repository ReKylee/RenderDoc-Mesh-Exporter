import { build } from 'esbuild';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = path.dirname(fileURLToPath(import.meta.url));
await mkdir(path.join(root, 'vendor'), { recursive: true });
await build({ entryPoints: [path.join(root, 'viewer.js')], bundle: true, format: 'iife', minify: true,
  target: ['es2020'], outfile: path.join(root, 'vendor', 'viewer.bundle.js') });
const license = await readFile(path.join(root, 'node_modules', 'three', 'LICENSE'), 'utf8');
await writeFile(path.join(root, 'vendor', 'THIRD_PARTY_LICENSES.txt'), 'Three.js and included addons\n\n' + license);
console.log('Built the offline Three.js viewer. Node is not needed to run the extension.');
