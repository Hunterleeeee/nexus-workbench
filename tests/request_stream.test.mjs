import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../static/request.js", import.meta.url), "utf8");

function loadClient(chunks) {
  const encoder = new TextEncoder();
  const queue = chunks.map((chunk) => encoder.encode(chunk));
  const window = {
    setTimeout,
    clearTimeout,
  };
  const context = {
    AbortController,
    Error,
    JSON,
    Set,
    String,
    TextDecoder,
    fetch: async () => ({
      ok: true,
      body: {
        getReader() {
          return {
            async read() {
              if (!queue.length) return { done: true, value: undefined };
              return { done: false, value: queue.shift() };
            },
          };
        },
      },
    }),
    window,
  };
  vm.runInNewContext(source, context, { filename: "static/request.js" });
  return window.WorkbenchUX;
}

test("fetchStream rejects an error event followed by DONE without finish", async () => {
  const client = loadClient([
    'data: {"type":"error","message":"provider failed","recoverable":false}\n\n',
    "data: [DONE]\n\n",
  ]);
  const errors = [];

  await assert.rejects(
    client.fetchStream("/api/chat", {}, { onError: (message) => errors.push(message) }),
    (error) => {
      assert.equal(error.name, "WorkbenchRequestError");
      assert.equal(error.code, "stream_failed");
      assert.equal(error.message, "provider failed");
      return true;
    },
  );
  assert.deepEqual(errors, ["provider failed"]);
});

test("fetchStream discards primary partial output after reset", async () => {
  const client = loadClient([
    'data: {"type":"delta","text":"primary partial"}\n\n',
    'data: {"type":"error","message":"retrying","recoverable":true}\n\n',
    'data: {"type":"reset"}\n\n',
    'data: {"type":"delta","text":"fallback answer"}\n\n',
    'data: {"type":"finish"}\n\ndata: [DONE]\n\n',
  ]);
  let visible = "";

  const result = await client.fetchStream("/api/chat", {}, {
    onDelta: (text) => { visible += text; },
    onReset: () => { visible = ""; },
  });

  assert.equal(result, "fallback answer");
  assert.equal(visible, "fallback answer");
});
