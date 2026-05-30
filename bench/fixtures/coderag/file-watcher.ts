import * as fs from "node:fs";
import * as path from "node:path";

export interface WatcherOptions {
  recursive?: boolean;
  filter?: (p: string) => boolean;
}

export type WatchEvent = "create" | "modify" | "delete";

export interface WatcherCallback {
  (event: WatchEvent, filePath: string): void;
}

/**
 * A file watcher that monitors a directory for changes.
 * Supports recursive watching and path filtering.
 */
export class FileWatcher {
  private watchers: fs.FSWatcher[] = [];
  private dir: string;
  private options: WatcherOptions;

  constructor(dir: string, options: WatcherOptions = {}) {
    this.dir = dir;
    this.options = options;
  }

  watch(callback: WatcherCallback): void {
    const watcher = fs.watch(this.dir, { recursive: this.options.recursive }, (event, filename) => {
      if (!filename) return;
      const fullPath = path.join(this.dir, filename);
      if (this.options.filter && !this.options.filter(fullPath)) return;
      const watchEvent: WatchEvent = event === "rename"
        ? (fs.existsSync(fullPath) ? "create" : "delete")
        : "modify";
      callback(watchEvent, fullPath);
    });
    this.watchers.push(watcher);
  }

  close(): void {
    this.watchers.forEach((w) => w.close());
    this.watchers = [];
  }
}
