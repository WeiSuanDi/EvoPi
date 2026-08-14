const status = document.querySelector("#status");
const output = document.querySelector("#output");
const runButton = document.querySelector("#run");
const abortButton = document.querySelector("#abort");
let socket;
let activeRunId;
const pending = new Map();

function append(value) {
  output.textContent += `${value}\n`;
}

function sendRpc(method, params) {
  const requestId = crypto.randomUUID();
  socket.send(JSON.stringify({ schema_version: 2, request_id: requestId, method, params }));
  return new Promise((resolve, reject) => pending.set(requestId, { resolve, reject }));
}

function sendRemote(type, data) {
  const requestId = crypto.randomUUID();
  socket.send(JSON.stringify({ schema_version: 1, type, request_id: requestId, data }));
  return new Promise((resolve, reject) => pending.set(requestId, { resolve, reject }));
}

function receive(event) {
  if (typeof event.data !== "string") return;
  const message = JSON.parse(event.data);
  if (message.schema_version === 2 && message.event_id) {
    if (message.type === "message_update" && message.data.kind === "text") {
      output.textContent += message.data.delta ?? "";
    } else if (message.type === "confirmation_state_changed") {
      append(`Confirmation ${message.data.request_id}: ${message.data.status}`);
    } else if (message.type === "agent_end") {
      activeRunId = undefined;
      abortButton.disabled = true;
      append(`\nRun ended: ${message.data.end_reason}`);
    }
    return;
  }
  const request = pending.get(message.request_id);
  if (!request) return;
  pending.delete(message.request_id);
  if (message.ok === false || message.type === "error") request.reject(new Error(message.error?.message ?? message.data?.message ?? "Request failed"));
  else request.resolve(message.result ?? message);
}

document.querySelector("#connect").addEventListener("click", async () => {
  const url = document.querySelector("#url").value;
  const deviceId = document.querySelector("#device-id").value.trim();
  if (!deviceId) { append("A paired Device ID is required."); return; }
  socket = new WebSocket(url, "evopi.remote.v1");
  socket.addEventListener("message", receive);
  socket.addEventListener("close", () => {
    status.textContent = "Disconnected";
    runButton.disabled = true;
    for (const request of pending.values()) request.reject(new Error("Connection closed"));
    pending.clear();
  });
  socket.addEventListener("open", async () => {
    try {
      const identity = await loadIdentity();
      if (!identity) throw new Error("Pair this browser before connecting.");
      const challengeFrame = await sendRemote("auth.begin", { device_id: deviceId });
      const signature = await signChallenge(identity.privateKey, challengeFrame.data.challenge);
      const authenticated = await sendRemote("auth.complete", { signature });
      await sendRpc("initialize", { client_name: "evopi-console", client_version: "1" });
      await sendRemote("lease.acquire", {});
      status.textContent = `Authenticated as ${deviceId}`;
      runButton.disabled = false;
      append(`Scopes: ${(authenticated.data.scopes ?? []).join(", ")}`);
    } catch (error) {
      append(error instanceof Error ? error.message : "Authentication failed");
      socket.close(1008, "authentication failed");
    }
  });
});

document.querySelector("#pair").addEventListener("click", async () => {
  const url = document.querySelector("#url").value;
  const code = document.querySelector("#pairing-code").value.trim();
  const deviceName = document.querySelector("#device-name").value.trim();
  const identity = await getOrCreateIdentity();
  socket = new WebSocket(url, "evopi.remote.v1");
  socket.addEventListener("message", receive);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  try {
    const result = await sendRemote("pairing.submit", {
      code,
      device_name: deviceName,
      public_jwk: identity.publicJwk,
    });
    append(`Pairing request ${result.data.request_id} is pending local approval.`);
  } finally {
    socket.close(1000, "pairing submitted");
  }
});

runButton.addEventListener("click", async () => {
  const prompt = document.querySelector("#prompt").value;
  const result = await sendRpc("run.start", { prompt });
  activeRunId = result.run_id;
  abortButton.disabled = false;
});

abortButton.addEventListener("click", async () => {
  if (activeRunId) await sendRpc("run.abort", { run_id: activeRunId });
});

async function getOrCreateIdentity() {
  const existing = await loadIdentity();
  if (existing) return existing;
  const generated = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]
  );
  const publicJwk = await crypto.subtle.exportKey("jwk", generated.publicKey);
  const privateJwk = await crypto.subtle.exportKey("jwk", generated.privateKey);
  const privateKey = await crypto.subtle.importKey(
    "jwk", privateJwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]
  );
  const identity = { publicJwk, privateKey };
  await storeIdentity(identity);
  return identity;
}

function openIdentityDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("evopi-remote", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("identity");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function loadIdentity() {
  const database = await openIdentityDatabase();
  return new Promise((resolve, reject) => {
    const request = database.transaction("identity").objectStore("identity").get("device");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storeIdentity(identity) {
  const database = await openIdentityDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction("identity", "readwrite");
    transaction.objectStore("identity").put(identity, "device");
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

async function signChallenge(privateKey, challenge) {
  const payload = new TextEncoder().encode(canonicalJson(challenge));
  const bytes = new Uint8Array(
    await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, payload)
  );
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}
