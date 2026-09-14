#!/usr/bin/env node
// Stable no-dependency entrypoint: node tools/check_web.mjs [web-directory]
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
const result = spawnSync(process.execPath, ["--experimental-vm-modules",
  fileURLToPath(new URL("./check_frontend.mjs", import.meta.url)), ...process.argv.slice(2)],
  { stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exitCode = result.status ?? 1;
