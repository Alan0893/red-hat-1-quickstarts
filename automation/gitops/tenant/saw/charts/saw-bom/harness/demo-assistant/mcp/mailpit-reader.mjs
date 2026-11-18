// Read-only Mailpit MCP server for the daily briefing.
import { createSourceContext, isMain, loadRuntime } from "./demo-lib.mjs";

export function createMailpitReader(config, dependencies = {}) {
  const { get, sourceHeaders, base, prefix, sourceTool } = createSourceContext(config, dependencies);
  return sourceTool("email", "saw_read_email", "Read the assigned demo email list and message bodies", async () => {
    const messages = [];
    const headers = sourceHeaders("MAILPIT_AUTHORIZATION");
    let start = 0;
    for (let page = 0; page < 20; page++) {
      const batch = await get(`${base("mailpit")}/api/v1/messages?start=${start}&limit=100`, { headers });
      if (!Array.isArray(batch.messages) || !Number.isInteger(batch.total)) throw new Error("Invalid Mailpit response");
      for (const item of batch.messages.filter(m => typeof m.Subject === "string" && m.Subject.startsWith(prefix))) {
        if (messages.length >= 50) throw new Error("More than 50 assigned emails; narrow the operator's fixture set");
        const url = `${base("mailpit")}/api/v1/message/${encodeURIComponent(item.ID)}`;
        const message = await get(url, { headers });
        if (typeof message.Subject !== "string" || !message.Subject.startsWith(prefix)) throw new Error("Mailpit message scope changed");
        messages.push({ id: item.ID, subject: message.Subject, from: message.From, received: message.Date,
          body: String(message.Text || "").slice(0, 10000), url });
      }
      start += batch.messages.length;
      if (start >= batch.total) return { source: "email", messages };
      if (!batch.messages.length) throw new Error("Incomplete Mailpit pagination");
    }
    throw new Error("Mailpit pagination exceeds the demo read limit");
  });
}

if (isMain(import.meta.url)) {
  const { config, briefing } = await loadRuntime();
  briefing.serve("mailpit_reader", [createMailpitReader(config, { briefing })]);
}
