const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
require('next');
require('sharp');
fs.accessSync('/app/server.js');
const packages = [];
const visited = new Set();
function inventory(directory) {
  const resolved = fs.realpathSync(directory);
  if (visited.has(resolved)) return;
  visited.add(resolved);
  const metadata = path.join(resolved, 'package.json');
  if (fs.existsSync(metadata)) {
    const item = JSON.parse(fs.readFileSync(metadata, 'utf8'));
    if (item.name && item.version) packages.push({ name: item.name, version: item.version, license: typeof item.license === 'string' ? item.license : undefined });
  }
  const modules = path.join(resolved, 'node_modules');
  if (!fs.existsSync(modules)) return;
  for (const name of fs.readdirSync(modules)) {
    if (name.startsWith('.')) continue;
    const child = path.join(modules, name);
    if (name.startsWith('@')) {
      for (const scoped of fs.readdirSync(child)) inventory(path.join(child, scoped));
    } else if (fs.statSync(child).isDirectory()) inventory(child);
  }
}
inventory('/app');
const child = spawn(process.execPath, ['/app/server.js'], {
  env: { ...process.env, HOSTNAME: '127.0.0.1', PORT: '3000' },
  stdio: ['ignore', 'ignore', 'inherit'],
});
(async () => {
  try {
    for (let i = 0; i < 60; i++) {
      if (child.exitCode !== null) throw new Error('Standalone server exited');
      try {
        const response = await fetch('http://127.0.0.1:3000/login');
        if (response.status === 200 && (await response.text()).includes('</html>')) {
          console.log(JSON.stringify({ login: 200, standalone: true, packages }));
          return;
        }
      } catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error('Standalone login page did not become ready');
  } finally {
    child.kill('SIGTERM');
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
