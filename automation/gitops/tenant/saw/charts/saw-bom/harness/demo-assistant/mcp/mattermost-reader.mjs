// Read-only Mattermost MCP server for the daily briefing.
import { createSourceContext, isMain, loadRuntime } from "./demo-lib.mjs";

export function createMattermostReader(config, dependencies = {}) {
  const { get, env, base, sourceTool } = createSourceContext(config, dependencies);
  const { tenantUser } = config;
  return sourceTool("chat", "saw_read_chat", "Read the latest messages from the assigned ai-platform chat channel", async () => {
    const placeholder = env.MM_TOKEN || env.MATTERMOST_TOKEN;
    if (!placeholder) throw new Error("Mattermost provider placeholder is unavailable");
    const headers = { Authorization: `Bearer ${placeholder}` };
    const teams = await get(`${base("mattermost")}/api/v4/users/me/teams`, { headers });
    const team = Array.isArray(teams) && teams.find(t => t.name === tenantUser && t.type === "I");
    if (!team) throw new Error("Mattermost assigned private team is unavailable");
    const channel = await get(`${base("mattermost")}/api/v4/teams/${encodeURIComponent(team.id)}/channels/name/ai-platform`, { headers });
    if (!/^[a-z0-9]{26}$/.test(channel?.id || "")) throw new Error("Invalid assigned channel");
    const assignedChannel = channel.id;
    const url = `${base("mattermost")}/api/v4/channels/${assignedChannel}/posts?per_page=100&page=0`;
    const data = await get(url, { headers });
    if (!Array.isArray(data.order) || !data.posts) throw new Error("Invalid Mattermost response");
    return { source: "chat", channelId: assignedChannel, url, limit: 100, messages: data.order.map(id => {
      const post = data.posts[id];
      if (!post) throw new Error("Incomplete Mattermost response");
      return { id, text: String(post.message || "").slice(0, 10000), createdAt: post.create_at };
    }) };
  });
}

if (isMain(import.meta.url)) {
  const { config, briefing } = await loadRuntime();
  briefing.serve("mattermost_reader", [createMattermostReader(config, { briefing })]);
}
