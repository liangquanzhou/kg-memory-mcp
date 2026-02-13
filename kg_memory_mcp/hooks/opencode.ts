/**
 * kg-memory-mcp OpenCode plugin
 *
 * Listens for session.idle events and triggers incremental conversation archival.
 * Install: kg-memory-mcp hooks install opencode
 * The CLI copies this file to ~/.config/opencode/plugins/kg-memory.ts
 */
import type { Plugin } from "@opencode-ai/plugin"

export const KgMemoryPlugin: Plugin = async ({ $ }) => {
  let lastArchived = 0

  return {
    "session.idle": async (_input, _output) => {
      // Debounce: skip if last archival was < 2 seconds ago
      const now = Date.now()
      if (now - lastArchived < 2000) return
      lastArchived = now

      try {
        await $`kg-memory-mcp hooks run opencode`
      } catch {
        // Silently ignore errors to avoid disrupting the user
      }
    },
  }
}
