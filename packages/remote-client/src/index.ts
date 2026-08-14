export const REMOTE_SUBPROTOCOL = "evopi.remote.v1" as const;

export type DeviceScope = "observe" | "control" | "confirm";

export interface RemoteFrame<T extends Record<string, unknown> = Record<string, unknown>> {
  schema_version: 1;
  type: string;
  request_id: string;
  data: T;
}

export interface RpcV2Request {
  schema_version: 2;
  request_id: string;
  method: string;
  params: Record<string, unknown>;
}

export interface RpcV2Response {
  schema_version: 2;
  request_id: string;
  ok: boolean;
  result: Record<string, unknown> | null;
  error: { code: string; message: string; details: Record<string, unknown> } | null;
}

export interface RpcV2Event {
  schema_version: 2;
  event_id: string;
  stream_id: string;
  sequence: number;
  type: string;
  data: Record<string, unknown>;
  run_id: string | null;
  created_at: string;
}

export interface RpcEventCursor {
  stream_id: string;
  sequence: number;
}

export interface RemoteEventPage {
  stream_id: string;
  after_sequence: number;
  snapshot_latest: number;
  next_sequence: number;
  complete: boolean;
  events: RpcV2Event[];
}

export interface RpcConfirmationRecord {
  schema_version: 1;
  request: Record<string, unknown>;
  status: string;
  runtime_id: string;
  revision: number;
  response: Record<string, unknown> | null;
  updated_at: string;
}

export interface DeviceIdentity {
  publicJwk: JsonWebKey;
  privateKey: CryptoKey;
}

export class RemoteProtocolError extends Error {}
export class RemoteOutcomeUnknownError extends Error {}

export async function createDeviceIdentity(): Promise<DeviceIdentity> {
  const generated = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    true,
    ["sign", "verify"],
  );
  const publicJwk = await crypto.subtle.exportKey("jwk", generated.publicKey);
  const privateJwk = await crypto.subtle.exportKey("jwk", generated.privateKey);
  const privateKey = await crypto.subtle.importKey(
    "jwk",
    privateJwk,
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"],
  );
  return { publicJwk, privateKey };
}

export async function signChallenge(
  privateKey: CryptoKey,
  challenge: Record<string, unknown>,
): Promise<string> {
  const payload = new TextEncoder().encode(canonicalJson(challenge));
  const signature = new Uint8Array(
    await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, payload),
  );
  if (signature.byteLength !== 64) {
    throw new RemoteProtocolError("P-256 signature must use 64-byte r || s encoding");
  }
  return base64url(signature);
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortValue(value));
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortValue(item)]),
    );
  }
  return value;
}

function base64url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function requestId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

export async function submitPairing(
  url: string,
  code: string,
  deviceName: string,
  publicJwk: JsonWebKey,
): Promise<string> {
  const socket = new WebSocket(url, REMOTE_SUBPROTOCOL);
  await new Promise<void>((resolve, reject) => {
    socket.addEventListener("open", () => resolve(), { once: true });
    socket.addEventListener("error", () => reject(new RemoteProtocolError("WSS failed")), {
      once: true,
    });
  });
  const id = requestId();
  try {
    socket.send(
      JSON.stringify({
        schema_version: 1,
        type: "pairing.submit",
        request_id: id,
        data: { code, device_name: deviceName, public_jwk: publicJwk },
      }),
    );
    const frame = await new Promise<RemoteFrame>((resolve, reject) => {
      socket.addEventListener(
        "message",
        (event) => {
          if (typeof event.data !== "string") {
            reject(new RemoteProtocolError("text frame required"));
            return;
          }
          resolve(JSON.parse(event.data) as RemoteFrame);
        },
        { once: true },
      );
      socket.addEventListener(
        "close",
        () => reject(new RemoteProtocolError("remote connection closed")),
        { once: true },
      );
    });
    const pendingId = frame.data.request_id;
    if (frame.type !== "pairing.pending" || frame.request_id !== id || typeof pendingId !== "string") {
      throw new RemoteProtocolError("invalid pairing response");
    }
    return pendingId;
  } finally {
    socket.close(1000, "pairing submitted");
  }
}

export class EvoPiRemoteClient extends EventTarget {
  readonly socket: WebSocket;
  readonly deviceId: string;
  scopes: readonly DeviceScope[] = [];
  serverInfo: Record<string, unknown> | undefined;
  private readonly url: string;
  private readonly privateKey: CryptoKey;
  private readonly eventQueue: RpcV2Event[] = [];
  private readonly eventWaiters: Array<{
    resolve: (value: RpcV2Event) => void;
    reject: (reason: unknown) => void;
  }> = [];
  private readonly pending = new Map<
    string,
    { resolve: (value: unknown) => void; reject: (reason: unknown) => void; sideEffect: boolean }
  >();

  private constructor(socket: WebSocket, url: string, deviceId: string, privateKey: CryptoKey) {
    super();
    this.socket = socket;
    this.url = url;
    this.deviceId = deviceId;
    this.privateKey = privateKey;
    socket.addEventListener("message", (event) => this.receive(event.data));
    socket.addEventListener("close", () => this.disconnect());
  }

  static async connect(
    url: string,
    deviceId: string,
    privateKey: CryptoKey,
  ): Promise<EvoPiRemoteClient> {
    const socket = new WebSocket(url, REMOTE_SUBPROTOCOL);
    socket.binaryType = "arraybuffer";
    await new Promise<void>((resolve, reject) => {
      socket.addEventListener("open", () => resolve(), { once: true });
      socket.addEventListener("error", () => reject(new RemoteProtocolError("WSS failed")), {
        once: true,
      });
    });
    const client = new EvoPiRemoteClient(socket, url, deviceId, privateKey);
    const challenge = await client.remoteRequest("auth.begin", { device_id: deviceId });
    if (challenge.type !== "auth.challenge" || typeof challenge.data.challenge !== "object") {
      throw new RemoteProtocolError("invalid auth challenge");
    }
    const signature = await signChallenge(
      privateKey,
      challenge.data.challenge as Record<string, unknown>,
    );
    const authenticated = await client.remoteRequest("auth.complete", { signature });
    if (authenticated.type !== "auth.ok" || !Array.isArray(authenticated.data.scopes)) {
      throw new RemoteProtocolError("device authentication failed");
    }
    client.scopes = authenticated.data.scopes as DeviceScope[];
    return client;
  }

  async initialize(clientName = "evopi-typescript", clientVersion = "0.1.0") {
    const result = await this.rpcRequest("initialize", {
      client_name: clientName,
      client_version: clientVersion,
    });
    this.serverInfo = result;
    return result;
  }

  async acquireControl() {
    return this.remoteRequest("lease.acquire", {}, true);
  }

  async renewControl() {
    return this.remoteRequest("lease.renew", {}, true);
  }

  async releaseControl() {
    return this.remoteRequest("lease.release", {}, true);
  }

  async startRun(prompt: string) {
    return this.rpcRequest("run.start", { prompt }, true);
  }

  async steer(runId: string, content: string) {
    return this.rpcRequest("run.steer", { run_id: runId, content }, true);
  }

  async followUp(runId: string, content: string) {
    return this.rpcRequest("run.follow_up", { run_id: runId, content }, true);
  }

  async abort(runId: string) {
    return this.rpcRequest("run.abort", { run_id: runId }, true);
  }

  async respondConfirmation(answer: Record<string, unknown>) {
    return this.rpcRequest("confirmation.respond", answer, true);
  }

  async listConfirmations(): Promise<readonly RpcConfirmationRecord[]> {
    const result = await this.rpcRequest("confirmation.list", {});
    if (!Array.isArray(result.pending)) {
      throw new RemoteProtocolError("invalid confirmation list");
    }
    return result.pending as RpcConfirmationRecord[];
  }

  async replayEvents(after: RpcEventCursor): Promise<RemoteEventPage> {
    const frame = await this.remoteRequest("events.page", {
      stream_id: after.stream_id,
      after_sequence: after.sequence,
    });
    if (frame.type !== "events.page" || !Array.isArray(frame.data.events)) {
      throw new RemoteProtocolError("invalid Remote event page");
    }
    return frame.data as unknown as RemoteEventPage;
  }

  async *events(): AsyncGenerator<RpcV2Event> {
    while (this.socket.readyState !== WebSocket.CLOSED || this.eventQueue.length > 0) {
      if (this.eventQueue.length > 0) {
        yield this.eventQueue.shift() as RpcV2Event;
        continue;
      }
      yield await new Promise<RpcV2Event>((resolve, reject) => {
        this.eventWaiters.push({ resolve, reject });
      });
    }
  }

  async *resilientEvents(after: RpcEventCursor): AsyncGenerator<RpcV2Event> {
    let client: EvoPiRemoteClient = this;
    let cursor = after;
    for (;;) {
      try {
        let page: RemoteEventPage;
        do {
          page = await client.replayEvents(cursor);
          for (const event of page.events) {
            if (event.stream_id !== cursor.stream_id || event.sequence <= cursor.sequence) continue;
            cursor = { stream_id: event.stream_id, sequence: event.sequence };
            yield event;
          }
        } while (!page.complete);
        for await (const event of client.events()) {
          if (event.stream_id !== cursor.stream_id) {
            throw new RemoteProtocolError("Remote Host event stream changed");
          }
          if (event.sequence <= cursor.sequence) continue;
          cursor = { stream_id: event.stream_id, sequence: event.sequence };
          yield event;
        }
      } catch (error) {
        if (!(error instanceof RemoteProtocolError) || error.message !== "remote connection closed") {
          throw error;
        }
      }
      client = await EvoPiRemoteClient.connect(this.url, this.deviceId, this.privateKey);
      const initialized = await client.initialize();
      const stream = initialized.stream as Record<string, unknown> | undefined;
      if (stream?.stream_id !== cursor.stream_id) {
        client.close();
        throw new RemoteProtocolError("Remote Host event stream changed");
      }
    }
  }

  close(): void {
    this.socket.close(1000, "client closed");
  }

  private async remoteRequest(
    type: string,
    data: Record<string, unknown>,
    sideEffect = false,
  ): Promise<RemoteFrame> {
    const id = requestId();
    const promise = this.pendingPromise(id, sideEffect);
    this.socket.send(JSON.stringify({ schema_version: 1, type, request_id: id, data }));
    return (await promise) as RemoteFrame;
  }

  private async rpcRequest(
    method: string,
    params: Record<string, unknown>,
    sideEffect = false,
  ): Promise<Record<string, unknown>> {
    const id = requestId();
    const promise = this.pendingPromise(id, sideEffect);
    const request: RpcV2Request = { schema_version: 2, request_id: id, method, params };
    this.socket.send(JSON.stringify(request));
    const response = (await promise) as RpcV2Response;
    if (!response.ok || response.result === null) {
      throw new RemoteProtocolError(response.error?.message ?? "RPC request failed");
    }
    return response.result;
  }

  private pendingPromise(id: string, sideEffect: boolean): Promise<unknown> {
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, sideEffect });
    });
  }

  private receive(data: unknown): void {
    if (typeof data !== "string") {
      this.socket.close(1003, "text frames required");
      return;
    }
    const value = JSON.parse(data) as Record<string, unknown>;
    if (value.schema_version === 2 && "event_id" in value) {
      const event = value as unknown as RpcV2Event;
      const waiter = this.eventWaiters.shift();
      if (waiter === undefined) this.eventQueue.push(event);
      else waiter.resolve(event);
      this.dispatchEvent(new CustomEvent("event", { detail: event }));
      return;
    }
    const id = value.request_id;
    if (typeof id !== "string") return;
    const pending = this.pending.get(id);
    if (pending === undefined) return;
    this.pending.delete(id);
    pending.resolve(value);
  }

  private disconnect(): void {
    for (const request of this.pending.values()) {
      request.reject(
        request.sideEffect
          ? new RemoteOutcomeUnknownError("remote operation outcome is unknown")
          : new RemoteProtocolError("remote connection closed"),
      );
    }
    this.pending.clear();
    const failure = new RemoteProtocolError("remote connection closed");
    for (const waiter of this.eventWaiters) waiter.reject(failure);
    this.eventWaiters.length = 0;
  }
}
