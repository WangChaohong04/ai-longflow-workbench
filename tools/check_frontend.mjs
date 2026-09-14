#!/usr/bin/env node
// Offline syntax and module-link checks; never evaluates application code.
// Usage: node --experimental-vm-modules tools/check_frontend.mjs [web-directory]
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

export async function checkFrontend(root) {
  if (!vm.SourceTextModule) throw new Error("Run Node with --experimental-vm-modules (Node 18+).");
  root = path.resolve(root);
  const modules = new Map();
  const context = vm.createContext({});
  function load(filename) {
    filename = path.resolve(filename);
    const relative = path.relative(root, filename);
    if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error(`Import outside web root: ${filename}`);
    if (!modules.has(filename)) {
      const source = readFileSync(filename, "utf8");
      try {
        modules.set(filename, new vm.SourceTextModule(source, { identifier: filename, context }));
      } catch (err) {
        throw new SyntaxError(`${relative}: ${err.message}`, { cause: err });
      }
    }
    return modules.get(filename);
  }
  function walk(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const filename = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(filename);
      else if (entry.isFile() && /\.(?:js|mjs)$/.test(entry.name)) load(filename);
    }
  }
  walk(root);
  const html = readFileSync(path.join(root, "index.html"), "utf8").replace(/<!--[\s\S]*?-->/g, "");
  const entries = [];
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
    const attrs = new Map([...match[1].matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)]
      .map(m => [m[1].toLowerCase(), m[2] ?? m[3] ?? m[4]]));
    if (attrs.get("type") !== "module") throw new Error("Workbench scripts must use type=module");
    const src = attrs.get("src");
    if (!src || match[2].trim()) throw new Error("Workbench scripts must use external module files");
    if (/^(?:[a-z]+:|\/)/i.test(src)) throw new Error(`Non-local script: ${src}`);
    const filename = path.resolve(root, src);
    if (entries.includes(filename)) throw new Error(`Duplicate script entry: ${src}`);
    entries.push(filename);
    load(filename);
  }
  if (entries.length !== 1 || entries[0] !== path.join(root, "app.js")) {
    throw new Error("index.html must load app.js exactly once as its sole module entry");
  }
  async function linker(specifier, referencing) {
    if (!specifier.startsWith("./") && !specifier.startsWith("../")) {
      throw new Error(`Non-relative static import: ${specifier} in ${referencing.identifier}`);
    }
    return load(path.resolve(path.dirname(referencing.identifier), specifier));
  }
  for (const mod of modules.values()) if (mod.status === "unlinked") await mod.link(linker);
  return { modules: modules.size, entries: entries.length };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const root = process.argv[2] || fileURLToPath(new URL("../longflow/web/", import.meta.url));
    if (!statSync(root).isDirectory()) throw new Error("Expected a web directory");
    const result = await checkFrontend(root);
    console.log(`Frontend static checks passed: ${result.modules} modules, ${result.entries} entry; syntax and imports/exports linked (not executed).`);
  } catch (err) {
    console.error(`Frontend static check failed: ${err.stack || err}`);
    process.exitCode = 1;
  }
}
