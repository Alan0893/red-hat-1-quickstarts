// Use the existing fixed-path writer with the same store as the demo readers.
import { briefingDir } from "./demo-lib.mjs";
process.env.BRIEFING_DIR ||= briefingDir();
await import("./briefing-writer.mjs");
