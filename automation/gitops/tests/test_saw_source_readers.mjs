import assert from "node:assert/strict";
import test, { after } from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createMailpitReader } from "../tenant/saw/charts/saw-bom/harness/demo-assistant/mcp/mailpit-reader.mjs";
import { createMattermostReader } from "../tenant/saw/charts/saw-bom/harness/demo-assistant/mcp/mattermost-reader.mjs";
import { createCalendarReader } from "../tenant/saw/charts/saw-bom/harness/demo-assistant/mcp/calendar-reader.mjs";

// Assemble the independently exported tools only for shared behavior checks.
function createSourceTools(config, dependencies = {}) {
  return [createMailpitReader(config, dependencies), createMattermostReader(config, dependencies),
    createCalendarReader(config, dependencies)];
}

const config = { clusterDomain: "test.example", demoId: "rh1", tenantUser: "rh1" };
function tools(read, env = { MAILPIT_AUTHORIZATION: "mail-marker", CALENDAR_AUTHORIZATION: "cal-marker" }) {
  return createSourceTools(config, { read, env });
}
const result = async (tool, args = {}) => tool.run(args);

const briefingRoot = await mkdtemp(join(tmpdir(), "saw-briefing-"));
const previousBriefingDirectory = process.env.BRIEFING_DIR;
process.env.BRIEFING_DIR = briefingRoot;
const briefingStore = await import("../tenant/saw/charts/saw-bom/harness/daily-briefing/mcp/briefing-lib.mjs");
if (previousBriefingDirectory === undefined) delete process.env.BRIEFING_DIR;
else process.env.BRIEFING_DIR = previousBriefingDirectory;
after(() => rm(briefingRoot, { recursive: true, force: true }));

test("briefing collection persists new messages across process restarts and day changes", async () => {
  let day = "2026-11-17";
  const messages = [{ ID: "first", Subject: "[SAW:rh1] Planning", Text: "Bring the report." }];
  const dependencies = {
    briefing: { ...briefingStore, today: () => ({ day }) },
    env: { MAILPIT_AUTHORIZATION: "mail-marker" },
    read: async url => new URL(url).pathname === "/api/v1/messages"
      ? { total: messages.length, messages }
      : messages.find(m => new URL(url).pathname.endsWith("/" + m.ID)),
  };
  const read = () => result(createSourceTools(config, dependencies)[0]);
  const first = await read();
  assert.equal(first.new, 1);
  assert.equal(first.day, day);
  assert.equal(first.file, join(briefingRoot, day, "items.jsonl"));
  assert.equal((await read()).new, 0);
  messages.push({ ID: "second", Subject: "[SAW:rh1] Deadline", Text: "Marco owns Monday." });
  const update = await read();
  assert.equal(update.new, 1);
  assert.equal(update.messages.length, 2, "Source-only questions still receive a fresh full snapshot");
  const stored = (await readFile(update.file, "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(stored.map(item => item.id), ["email:first", "email:second"]);
  assert.equal(stored[1].body, "Marco owns Monday.");
  day = "2026-11-18";
  assert.equal((await read()).new, 2, "A new day collects a complete baseline again");
});

test("failed source reads do not consume messages in the briefing store", async () => {
  let fails = true;
  const tenant = { ...config, demoId: "retry", tenantUser: "retry" };
  const dependencies = {
    briefing: { ...briefingStore, today: () => ({ day: "2026-11-19" }) },
    env: { MAILPIT_AUTHORIZATION: "mail-marker" },
    read: async url => {
      if (new URL(url).pathname === "/api/v1/messages")
        return { total: 1, messages: [{ ID: "recover", Subject: "[SAW:retry] Planning" }] };
      if (fails) throw new Error("source unavailable");
      return { Subject: "[SAW:retry] Planning", Text: "Recovered body" };
    },
  };
  await assert.rejects(() => result(createSourceTools(tenant, dependencies)[0]), /source unavailable/);
  fails = false;
  assert.equal((await result(createSourceTools(tenant, dependencies)[0])).new, 1);
});

test("calendar collection records changes without duplicating unchanged snapshots", async () => {
  let calendar = "BEGIN:VCALENDAR\r\nSUMMARY:Planning\r\nEND:VCALENDAR";
  const dependencies = {
    briefing: { ...briefingStore, today: () => ({ day: "2026-11-20" }) },
    env: { CALENDAR_AUTHORIZATION: "cal-marker" }, read: async () => calendar,
  };
  const read = () => result(createSourceTools(config, dependencies)[2]);
  assert.equal((await read()).new, 1);
  assert.equal((await read()).new, 0);
  calendar = calendar.replace("Planning", "Updated planning");
  const changed = await read();
  assert.equal(changed.new, 1);
  const snapshots = (await readFile(changed.file, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots.at(-1).calendar, calendar);
});

test("email reads only assigned subjects, follows pagination, and fetches bodies", async () => {
  const requests = [];
  const list = tools(async (url) => {
    requests.push(url);
    const u = new URL(url);
    if (u.pathname === "/api/v1/messages") return { total: 2, messages: u.searchParams.get("start") === "0"
      ? [{ ID: "other", Subject: "[SAW:other] private" }]
      : [{ ID: "owned", Subject: "[SAW:rh1] Planning" }] };
    assert.equal(u.pathname, "/api/v1/message/owned");
    return { ID: "owned", Subject: "[SAW:rh1] Planning", Text: "Marco owns the load-test plan." };
  });
  const data = await result(list[0]);
  assert.equal(data.messages.length, 1);
  assert.match(data.messages[0].body, /Marco/);
  assert.ok(requests.every(u => new URL(u).hostname === "mailpit-ui-openshell-agents.apps.test.example"));
  assert.equal(requests.length, 3);
});

test("calendar returns the assigned collection's ICS without guessing dates", async () => {
  const list = tools(async (url, options) => {
    assert.equal(new URL(url).pathname, "/rh1/rh1/");
    assert.equal(options.format, "text");
    return "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261113T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR";
  });
  assert.match((await result(list[2])).calendar, /20261113T100000Z/);
});

test("model arguments cannot override the host, method, channel or credentials", async () => {
  const list = tools(() => assert.fail("unexpected network request"));
  for (const tool of list) await assert.rejects(() => result(tool, { url: "https://attacker.invalid", method: "POST" }), /arguments/);
  await assert.rejects(() => result(list[1]), /Mattermost provider/);
});

test("configuration rejects URL and path injection", async () => {
  for (const patch of [{ clusterDomain: "test.example/collect" }, { demoId: "../other" }, { tenantUser: "../other" }]) {
    assert.throws(() => createSourceTools({ ...config, ...patch }), /source-reader configuration/);
  }
});

test("tenant readers authenticate fixed sources and resolve only the assigned private team", async () => {
  const tenant = { ...config, demoId: "user-test", tenantUser: "user-test" };
  const calls = [];
  // A migrated provider may retain its old alias alongside the canonical key.
  const list = createSourceTools(tenant, { env: { MM_TOKEN: "chat-marker", MATTERMOST_TOKEN: "stale-chat-marker", MAILPIT_AUTHORIZATION: "mail-marker", CALENDAR_AUTHORIZATION: "cal-marker" },
    read: async (url, options) => {
      calls.push(url);
      const u = new URL(url);
      if (u.hostname.startsWith("mailpit")) {
        assert.equal(options.headers.Authorization, "mail-marker");
        return { messages: [], total: 0 };
      }
      if (u.hostname.startsWith("radicale")) {
        assert.equal(u.pathname, "/user-test/user-test/");
        assert.equal(options.headers.Authorization, "cal-marker");
        return "BEGIN:VCALENDAR\r\nEND:VCALENDAR";
      }
      assert.equal(options.headers.Authorization, "Bearer chat-marker");
      if (u.pathname === "/api/v4/users/me/teams") return [{ id: "team-id", name: "user-test", type: "I" }, { id: "other", name: "other", type: "I" }];
      if (u.pathname === "/api/v4/teams/team-id/channels/name/ai-platform") return { id: "a".repeat(26) };
      assert.equal(u.pathname, `/api/v4/channels/${"a".repeat(26)}/posts`);
      return { order: [], posts: {} };
    } });
  for (const t of list.slice(0,3)) await result(t);
  assert.equal(calls.length, 5);
  const denied = createSourceTools(tenant, { env: {} });
  await assert.rejects(() => result(denied[0]), /provider placeholder/);
  await assert.rejects(() => result(denied[2]), /provider placeholder/);
  const wrongTeam = createSourceTools(tenant, { env: { MATTERMOST_TOKEN: "marker" }, read: async () => [{id: "other", name:"other", type:"I"}] });
  await assert.rejects(() => result(wrongTeam[1]), /assigned private team/);
});

test("parallel source tools serialize requests to avoid overlapping Keycloak password grants", async () => {
  let active = false;
  const list = createSourceTools({ ...config, demoId: "user-test", tenantUser: "user-test" }, {
    env: { MAILPIT_AUTHORIZATION: "mail-marker", CALENDAR_AUTHORIZATION: "cal-marker" },
    read: async url => {
      assert.equal(active, false, "The identity server rejects concurrent password grants for one user");
      active = true;
      await new Promise(resolve => setImmediate(resolve));
      active = false;
      return new URL(url).hostname.startsWith("mailpit")
        ? { messages: [], total: 0 } : "BEGIN:VCALENDAR\r\nEND:VCALENDAR";
    },
  });
  const data = await Promise.all([result(list[0]), result(list[2])]);
  assert.deepEqual(data.map(item => item.source), ["email", "calendar"]);
});

test("a rejected source request does not prevent the next queued source from running", async () => {
  const list = tools(async url => {
    if (new URL(url).hostname.startsWith("mailpit")) throw new Error("identity unavailable");
    return "BEGIN:VCALENDAR\r\nEND:VCALENDAR";
  });
  const data = await Promise.allSettled([result(list[0]), result(list[2])]);
  assert.equal(data[0].status, "rejected");
  assert.equal(data[1].status, "fulfilled");
  assert.equal(data[1].value.source, "calendar");
});
