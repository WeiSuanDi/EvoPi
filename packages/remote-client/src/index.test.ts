import assert from "node:assert/strict";
import test from "node:test";

import { canonicalJson, createDeviceIdentity, signChallenge } from "./index.js";

test("canonical JSON sorts nested object keys", () => {
  assert.equal(canonicalJson({ z: 1, a: { d: 2, b: 1 } }), '{"a":{"b":1,"d":2},"z":1}');
});

test("device private key is non-exportable and signs fixed P-256 payloads", async () => {
  const identity = await createDeviceIdentity();
  assert.equal(identity.privateKey.extractable, false);
  const signature = await signChallenge(identity.privateKey, { protocol: "evopi.remote.v1" });
  assert.equal(signature.length, 86);
});
