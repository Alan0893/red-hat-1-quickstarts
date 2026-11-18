// Read-only Calendar MCP server for the daily briefing.
import { createSourceContext, isMain, loadRuntime } from "./demo-lib.mjs";

export function createCalendarReader(config, dependencies = {}) {
  const { get, sourceHeaders, base, sourceTool } = createSourceContext(config, dependencies);
  const { tenantUser, demoId } = config;
  return sourceTool("calendar", "saw_read_calendar", "Read the assigned demo calendar events and their UTC timestamps", async () => {
    const url = `${base("radicale")}/${tenantUser}/${demoId}/`;
    const calendar = await get(url, { format: "text", headers: sourceHeaders("CALENDAR_AUTHORIZATION") });
    if (typeof calendar !== "string" || !calendar.includes("BEGIN:VCALENDAR") || calendar.length > 100000) {
      throw new Error("Invalid or oversized calendar response");
    }
    return { source: "calendar", url, calendar };
  });
}

if (isMain(import.meta.url)) {
  const { config, briefing } = await loadRuntime();
  briefing.serve("calendar_reader", [createCalendarReader(config, { briefing })]);
}
