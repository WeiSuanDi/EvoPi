import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
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

test("shared Remote v1 conformance fixtures are consumable", async () => {
  const payload = JSON.parse(
    await readFile("../../tests/conformance/remote_v1/frames.json", "utf8"),
  ) as { schema_version: number; valid: Array<Record<string, unknown>> };

  assert.equal(payload.schema_version, 1);
  assert.equal(payload.valid.every((frame) => frame.schema_version === 1), true);
});
