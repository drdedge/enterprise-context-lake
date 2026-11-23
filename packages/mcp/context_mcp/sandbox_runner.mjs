// Enterprise Context Lake — Zero-Egress Node 24 --permission Code Mode Runner
// Launched with: node --permission --max-old-space-size=64 sandbox_runner.mjs
// ZERO --allow-fs-*, ZERO --allow-child-process, ZERO --allow-worker, ZERO --allow-addons,
// and stripped environment variables (no secrets in process.env).

import * as readline from "node:readline";
import * as vm from "node:vm";

// Capture real stdio channels privately before scrubbing globals.
const stdin = process.stdin;
const stdout = process.stdout;
const exit = process.exit.bind(process);

// Defense-in-depth: remove network & browser-like globals from the isolate realm.
for (const key of ["fetch", "WebSocket", "XMLHttpRequest", "EventSource", "Request", "Response", "Headers"]) {
  try {
    Object.defineProperty(globalThis, key, {
      value: undefined,
      writable: false,
      configurable: false,
    });
  } catch {
    // Ignore non-configurable properties
  }
}

const MAX_LOG_ENTRIES = 200;
const MAX_LOG_BYTES = 65536;

function sendFrame(obj) {
  stdout.write(JSON.stringify(obj) + "\n");
}

function safeFormatArg(arg) {
  if (typeof arg === "string") return arg;
  try {
    return JSON.stringify(arg);
  } catch {
    return String(arg);
  }
}

const pending = new Map();
let nextRpcId = 1;
let started = false;

function invokeHostRpc(method, args) {
  return new Promise((resolve, reject) => {
    const id = nextRpcId++;
    pending.set(id, { resolve, reject });
    let serializedArgs;
    try {
      serializedArgs = JSON.parse(JSON.stringify(args));
    } catch (err) {
      pending.delete(id);
      reject(new Error(`RPC arguments must be JSON-serializable: ${err.message}`));
      return;
    }
    sendFrame({ type: "rpc", id, method, args: serializedArgs });
  });
}

async function runUserCode(frame) {
  const userCode = String(frame.code ?? "");
  const timeoutMs = Math.min(Math.max(Number(frame.timeout_ms || 3000), 100), 10000);
  const allowedMethods = Array.isArray(frame.allowed_methods) ? frame.allowed_methods : [];

  const logs = [];
  let logBytes = 0;

  function appendLog(level, args) {
    if (logs.length >= MAX_LOG_ENTRIES || logBytes >= MAX_LOG_BYTES) return;
    const line = args.map(safeFormatArg).join(" ");
    const entry = level === "log" ? line : `[${level}] ${line}`;
    logBytes += Buffer.byteLength(entry, "utf8");
    logs.push(entry);
  }

  // Create an isolated VM context with string code generation and WASM disabled.
  // This blocks eval() and Function("...") escapes from inside the sandbox.
  const context = vm.createContext(Object.create(null), {
    name: "context-lake-codemode-sandbox",
    codeGeneration: { strings: false, wasm: false },
  });

  // Build realm-local helpers inside the VM so returned values and errors never
  // expose host-realm Function constructors.
  const bridgeInstaller = new vm.Script(
    `(function(rawCall, rawLog, methods) {
      "use strict";
      const lakeObj = Object.create(null);
      for (const m of methods) {
        lakeObj[m] = function(...args) {
          let payload;
          try {
            payload = JSON.stringify(args);
          } catch (e) {
            return Promise.reject(new Error("Arguments must be JSON-serializable"));
          }
          return new Promise((resolve, reject) => {
            rawCall(
              m,
              payload,
              (okJson) => {
                try {
                  resolve(JSON.parse(okJson));
                } catch (e) {
                  reject(new Error("Invalid JSON from host RPC"));
                }
              },
              (errJson) => {
                try {
                  const info = JSON.parse(errJson);
                  const err = new Error(info.message || "RPC error");
                  if (info.code) err.code = info.code;
                  reject(err);
                } catch {
                  reject(new Error(String(errJson || "RPC error")));
                }
              }
            );
          });
        };
      }
      Object.freeze(lakeObj);

      const safeConsole = Object.freeze({
        log: (...a) => rawLog("log", JSON.stringify(a.map((x) => {
          if (typeof x === "string") return x;
          try { return JSON.parse(JSON.stringify(x)); } catch { return String(x); }
        }))),
        info: (...a) => rawLog("info", JSON.stringify(a.map((x) => {
          if (typeof x === "string") return x;
          try { return JSON.parse(JSON.stringify(x)); } catch { return String(x); }
        }))),
        warn: (...a) => rawLog("warn", JSON.stringify(a.map((x) => {
          if (typeof x === "string") return x;
          try { return JSON.parse(JSON.stringify(x)); } catch { return String(x); }
        }))),
        error: (...a) => rawLog("error", JSON.stringify(a.map((x) => {
          if (typeof x === "string") return x;
          try { return JSON.parse(JSON.stringify(x)); } catch { return String(x); }
        }))),
      });

      Object.defineProperty(globalThis, "lake", {
        value: lakeObj,
        writable: false,
        enumerable: true,
        configurable: false,
      });
      Object.defineProperty(globalThis, "console", {
        value: safeConsole,
        writable: false,
        enumerable: true,
        configurable: false,
      });
      // Explicitly shadow dangerous globals as undefined non-configurable properties
      for (const blocked of [
        "process", "require", "module", "exports", "__filename", "__dirname",
        "fetch", "WebSocket", "XMLHttpRequest", "EventSource",
        "setTimeout", "setInterval", "setImmediate", "clearTimeout", "clearInterval", "clearImmediate",
        "Buffer", "global"
      ]) {
        try {
          Object.defineProperty(globalThis, blocked, {
            value: undefined,
            writable: false,
            enumerable: false,
            configurable: false,
          });
        } catch {}
      }
    })`,
    { filename: "sandbox-bootstrap.js" }
  );

  const installFn = bridgeInstaller.runInContext(context, { timeout: 500 });

  const rawCall = (method, argsJson, onOk, onErr) => {
    let parsedArgs = [];
    try {
      parsedArgs = JSON.parse(String(argsJson));
    } catch (err) {
      onErr(JSON.stringify({ code: "validation_error", message: String(err.message || err) }));
      return;
    }
    invokeHostRpc(String(method), parsedArgs).then(
      (res) => {
        try {
          onOk(JSON.stringify(res === undefined ? null : res));
        } catch (err) {
          onErr(JSON.stringify({ code: "serialization_error", message: String(err.message || err) }));
        }
      },
      (err) => {
        onErr(
          JSON.stringify({
            code: err && err.code ? String(err.code) : "rpc_error",
            message: String(err && err.message ? err.message : err),
          })
        );
      }
    );
  };

  const rawLog = (level, argsJson) => {
    try {
      const arr = JSON.parse(String(argsJson));
      appendLog(String(level), Array.isArray(arr) ? arr : [arr]);
    } catch {
      appendLog(String(level), [String(argsJson)]);
    }
  };

  installFn(rawCall, rawLog, allowedMethods);

  // Compile and execute user code wrapped in an async IIFE.
  // Reject dynamic import() via importModuleDynamically hook.
  const wrapped = `(async () => {\n"use strict";\n${userCode}\n})()`;
  try {
    const script = new vm.Script(wrapped, {
      filename: "codemode-user.js",
      importModuleDynamically() {
        throw new Error("Dynamic import() is disabled in Code Mode sandbox");
      },
    });
    const promiseOrValue = script.runInContext(context, {
      timeout: timeoutMs,
      breakOnSigint: true,
    });

    // Enforce wall-clock timeout for async execution inside Node as well as Python host.
    let timer;
    const timeoutPromise = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const err = new Error(`Code Mode execution exceeded timeout (${timeoutMs} ms)`);
        err.name = "TimeoutError";
        reject(err);
      }, timeoutMs);
    });

    let value;
    try {
      value = await Promise.race([Promise.resolve(promiseOrValue), timeoutPromise]);
    } finally {
      clearTimeout(timer);
    }

    let safeValue = null;
    if (value !== undefined) {
      try {
        safeValue = JSON.parse(JSON.stringify(value));
      } catch {
        safeValue = String(value);
      }
    }
    sendFrame({ type: "result", ok: true, value: safeValue, logs });
    exit(0);
  } catch (err) {
    sendFrame({
      type: "result",
      ok: false,
      error: {
        name: String(err && err.name ? err.name : "Error"),
        code: err && err.code ? String(err.code) : undefined,
        message: String(err && err.message ? err.message : err),
      },
      logs,
    });
    exit(0);
  }
}

const rl = readline.createInterface({ input: stdin, crlfDelay: Infinity });

rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let frame;
  try {
    frame = JSON.parse(trimmed);
  } catch {
    return;
  }

  if (frame.type === "exec") {
    if (started) return;
    started = true;
    runUserCode(frame);
  } else if (frame.type === "rpc_result") {
    const entry = pending.get(frame.id);
    if (!entry) return;
    pending.delete(frame.id);
    if (frame.ok) {
      entry.resolve(frame.result);
    } else {
      const err = new Error(frame.error?.message || "Host RPC failed");
      if (frame.error?.code) err.code = frame.error.code;
      entry.reject(err);
    }
  }
});

rl.on("close", () => {
  exit(0);
});
