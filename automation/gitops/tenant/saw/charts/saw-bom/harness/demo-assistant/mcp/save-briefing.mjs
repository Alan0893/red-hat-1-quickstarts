// Use the existing fixed-path writer with the same store as the demo readers.
process.env.BRIEFING_DIR ||= "/sandbox/.openclaw/workspace/briefing";
await import("./briefing-writer.mjs");
