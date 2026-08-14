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

export class EvoPiRemoteClient extends EventTarget {
  readonly socket: WebSocket;
  readonly deviceId: string;
  scopes: readonly DeviceScope[] = [];
  private readonly pending = new Map<
    string,
    { resolve: (value: unknown) => void; reject: (reason: unknown) => void; sideEffect: boolean }
  >();

  private constructor(socket: WebSocket, deviceId: string) {
    super();
    this.socket = socket;
    this.deviceId = deviceId;
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
    const client = new EvoPiRemoteClient(socket, deviceId);
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
    return this.rpcRequest("initialize", { client_name: clientName, client_version: clientVersion });
  }

  async acquireControl() {
    return this.remoteRequest("lease.acquire", {}, true);
  }

  async renewControl() {
    return this.remoteRequest("lease.renew", {}, true);
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
      this.dispatchEvent(new CustomEvent("event", { detail: value as unknown as RpcV2Event }));
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
  }
}
