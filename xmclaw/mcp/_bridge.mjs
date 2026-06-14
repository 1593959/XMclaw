// XMCLaw MCP Bridge — Node.js thin wrapper for Python MCP server
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const PYTHON = "C:/Users/15978/Desktop/XMclaw/.venv/Scripts/python.exe";
const SCRIPT = "C:/Users/15978/Desktop/XMclaw/xmclaw/mcp/_serve.py";

const proc = spawn(PYTHON, [SCRIPT], {
  stdio: ["pipe", "pipe", "inherit"],
  env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
});

process.stdin.pipe(proc.stdin);
proc.stdout.pipe(process.stdout);

proc.on("exit", (code) => process.exit(code || 0));
proc.on("error", () => process.exit(1));
