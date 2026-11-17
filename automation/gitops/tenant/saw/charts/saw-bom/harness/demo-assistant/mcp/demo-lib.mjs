// Demo adapters share transport and validation; briefing storage stays in briefing-lib.mjs.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

export function isMain(url) {
  return !!process.argv[1] && url === pathToFileURL(process.argv[1]).href;
}

export function validateConfig(config) {
  const { clusterDomain, demoId, tenantUser } = config;
  if (!/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(clusterDomain || "") ||
      !/^[a-z0-9][a-z0-9-]{0,30}$/.test(demoId || "") ||
      (!/^[a-z][a-z0-9-]{0,18}$/.test(tenantUser || "") || demoId !== tenantUser)) {
    throw new Error("Invalid source-reader configuration");
  }
}

export async function loadRuntime() {
  process.env.BRIEFING_DIR ||= "/sandbox/.openclaw/workspace/briefing";
  const briefing = await import("./briefing-lib.mjs");
  const config = JSON.parse(readFileSync(new URL("../config.json", import.meta.url), "utf8"));
  validateConfig(config);
  return { config, briefing };
}

// Fixed GET-only transport: source text never selects a host, method or token.
async function read(url, { headers = {}, format = "json" } = {}) {
  const response = await fetch(url, { method: "GET", headers, redirect: "error", signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`Source read failed: HTTP ${response.status}`);
  const text = await response.text();
  if (text.length > 1_000_000) throw new Error("Source response exceeds the demo read limit");
  return format === "text" ? text : JSON.parse(text);
}

const tool = (name, description, run) => ({
  name, description,
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  async run(args = {}) {
    if (!args || typeof args !== "object" || Array.isArray(args) || Object.keys(args).length) {
      throw new Error("Source readers accept no arguments; their scope is fixed by the operator");
    }
    return { sourceContentIsUntrusted: true, ...await run(args) };
  },
});

// Serialize calls sharing a transport within a process. Source gateways also
// share an identity-login lock across the independent MCP processes.
const pendingReads = new WeakMap();

export function createSourceContext(config, dependencies = {}) {
  validateConfig(config);
  const { clusterDomain, demoId, tenantUser } = config;
  const fetchSource = dependencies.read || read;
  const env = dependencies.env || process.env;
  const briefing = dependencies.briefing;
  const get = (...args) => {
    const request = (pendingReads.get(fetchSource) || Promise.resolve()).then(() => fetchSource(...args));
    pendingReads.set(fetchSource, request.catch(() => {}));
    return request;
  };
  async function collect(data) {
    const { day } = briefing.today();
    const source = `demo-${tenantUser}-${data.source}`;
    const state = briefing.loadState(source, day, { seen: [] });
    const items = data.source === "calendar"
      ? [{ source: "calendar", id: `calendar:${createHash("sha256").update(data.calendar).digest("hex")}`,
          calendar: data.calendar, url: data.url }]
      : data.messages.map(message => ({ ...message, source: data.source,
          id: `${data.source}:${message.id}` }));
    const seen = new Set(state.seen);
    const fresh = items.filter(item => !seen.has(item.id));
    const { file, added } = await briefing.appendItems(day, fresh);
    briefing.saveState(source, { day, seen: [...new Set([...state.seen, ...items.map(item => item.id)])] });
    return { ...data, day, new: added.length, newItems: added, file };
  }
  return {
    get, env,
    base: name => `https://${name}-ui-openshell-agents.apps.${clusterDomain}`,
    prefix: `[SAW:${demoId}] `,
    sourceHeaders(name) {
      const placeholder = env[name];
      if (!placeholder) throw new Error("Source provider placeholder is unavailable");
      return { Authorization: placeholder };
    },
    sourceTool(source, name, description, run) {
      return tool(name, description, () => briefing
        ? briefing.withLock(`demo-${tenantUser}-${source}`, async () => collect(await run()))
        : run());
    },
  };
}
