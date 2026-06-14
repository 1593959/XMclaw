#!/usr/bin/env node
import { spawn } from "node:child_process";
const proc = spawn("C:/Users/15978/Desktop/XMclaw/.venv/Scripts/python.exe", ["C:/Users/15978/Desktop/XMclaw/xmclaw/mcp/_serve.py"], {
  stdio: ["pipe", "pipe", "inherit"],
  env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
});
process.stdin.pipe(proc.stdin);
proc.stdout.pipe(process.stdout);
proc.on("exit", (code) => process.exit(code || 0));
