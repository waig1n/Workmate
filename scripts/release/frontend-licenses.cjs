// Retain dependency notices that standalone output tracing may omit.
const fs = require('node:fs');
const path = require('node:path');
const source = path.resolve(process.argv[2]);
const destination = path.resolve(process.argv[3]);
function copyNotices(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) copyNotices(file);
    else if (entry.isFile() && (/^(license|licence|notice|copying)([.-]|$)/i.test(entry.name)
      || path.relative(source, file).split(path.sep).some(part => /^licen[cs]es$/i.test(part)))) {
      const target = path.join(destination, path.relative(source, file));
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(file, target);
    }
  }
}
fs.mkdirSync(destination, { recursive: true });
copyNotices(source);
