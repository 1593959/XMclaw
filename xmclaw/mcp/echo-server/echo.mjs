#!/usr/bin/env node
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  try { const msg = JSON.parse(line); const m = msg.method||""; const id = msg.id;
    if (m==="initialize") process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{protocolVersion:"2024-11-05",capabilities:{},serverInfo:{name:"echo-npx",version:"1.0.0"}}})+"\n");
    else if (m==="tools/list") process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{tools:[{name:"ping",description:"Ping-pong test",inputSchema:{type:"object",properties:{}}}]}})+"\n");
    else if (m==="tools/call") process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{content:[{type:"text",text:"pong"}]}})+"\n");
  } catch {}
});
