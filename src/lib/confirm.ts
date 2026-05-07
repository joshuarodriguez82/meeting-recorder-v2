/**
 * Tauri 2 dialog wrapper. Tauri's webview blocks browser-native
 * `window.confirm()` because it can't style native dialogs, so any call
 * site that needs a yes/no prompt has to go through the dialog plugin.
 * Falls back to `window.confirm` in non-Tauri contexts (e.g. plain
 * `next dev` in a browser without the Tauri shell) so dev workflows
 * outside Tauri still work.
 */
export async function confirmDialog(
  message: string,
  opts: { title?: string; kind?: "info" | "warning" | "error" } = {},
): Promise<boolean> {
  try {
    const { confirm } = await import("@tauri-apps/plugin-dialog");
    return await confirm(message, {
      title: opts.title ?? "Confirm",
      kind: opts.kind ?? "warning",
    });
  } catch {
    return window.confirm(message);
  }
}
