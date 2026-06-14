// Minimal MCP echo server for connectivity testing
import { createInterface } from "node:readline";

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  try {
    const msg = JSON.parse(line);
    const method = msg.method || "";
    const id = msg.id;
    if (method === "initialize") {
      process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{protocolVersion:"2024-11-05",capabilities:{},serverInfo:{name:"echo-test",version:"1.0.0"}}}) + "\n");
    } else if (method === "tools/list") {
      process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{tools:[{name:"ping",description:"Test tool",inputSchema:{type:"object",properties:{}}}]}}) + "\n");
    } else if (method === "tools/call") {
      process.stdout.write(JSON.stringify({jsonrpc:"2.0",id,result:{content:[{type:"text",text:"pong"}]}}) + "\n");
    }
  } catch {}
});
