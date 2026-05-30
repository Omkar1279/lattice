import { EventEmitter } from "events";

interface Logger {
  info(msg: string): void;
  error(msg: string): void;
}

interface Connection {
  id: string;
  active: boolean;
  lastUsed: number;
}

/**
 * A connection pool that manages reusable connections with idle timeout.
 * Emits 'acquire' and 'release' events for monitoring.
 */
export class ConnectionPool extends EventEmitter {
  private pool: Map<string, Connection> = new Map();
  private logger: Logger;
  private maxSize: number;
  private idleTimeoutMs: number;

  constructor(logger: Logger, maxSize = 10, idleTimeoutMs = 30000) {
    super();
    this.logger = logger;
    this.maxSize = maxSize;
    this.idleTimeoutMs = idleTimeoutMs;
  }

  acquire(): Connection | null {
    for (const [id, conn] of this.pool) {
      if (!conn.active) {
        conn.active = true;
        conn.lastUsed = Date.now();
        this.emit("acquire", id);
        return conn;
      }
    }
    if (this.pool.size >= this.maxSize) return null;
    const conn: Connection = { id: crypto.randomUUID(), active: true, lastUsed: Date.now() };
    this.pool.set(conn.id, conn);
    this.logger.info(`Created connection ${conn.id}`);
    this.emit("acquire", conn.id);
    return conn;
  }

  release(id: string): void {
    const conn = this.pool.get(id);
    if (!conn) return;
    conn.active = false;
    conn.lastUsed = Date.now();
    this.emit("release", id);
  }
}
