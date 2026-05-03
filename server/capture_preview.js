const os = require("os");
const path = require("path");
const fs = require("fs");
const http = require("http");
const https = require("https");
const { spawn } = require("child_process");

const LOCAL_PREVIEW_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);
const PREVIEW_START_TIMEOUT_MS =
  Number(process.env.WARROOM_PREVIEW_START_TIMEOUT || 25) * 1000;

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  ({ chromium } = require(path.join(
    os.homedir(),
    ".cache",
    "codex-runtimes",
    "codex-primary-runtime",
    "dependencies",
    "node",
    "node_modules",
    "playwright",
  )));
}

function localHttpUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return null;
  }
  if (!LOCAL_PREVIEW_HOSTS.has(parsed.hostname)) {
    return null;
  }
  return parsed;
}

function urlResponds(rawUrl, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const parsed = new URL(rawUrl);
    const client = parsed.protocol === "https:" ? https : http;
    const request = client.get(parsed, (response) => {
      response.resume();
      resolve(response.statusCode >= 100 && response.statusCode < 600);
    });
    request.on("error", () => resolve(false));
    request.setTimeout(timeoutMs, () => {
      request.destroy();
      resolve(false);
    });
  });
}

function projectRootFromOutput(outputPath) {
  const resolved = path.resolve(outputPath);
  const parts = resolved.split(path.sep);
  const marker = parts.lastIndexOf(".warroom");
  if (marker > 0) {
    return parts.slice(0, marker).join(path.sep);
  }
  return process.cwd();
}

function packageScripts(projectRoot) {
  const packagePath = path.join(projectRoot, "package.json");
  if (!fs.existsSync(packagePath)) {
    return {};
  }
  try {
    const data = JSON.parse(fs.readFileSync(packagePath, "utf8"));
    return data && typeof data.scripts === "object" ? data.scripts : {};
  } catch {
    return {};
  }
}

function previewCommand(projectRoot, parsedUrl) {
  const scripts = packageScripts(projectRoot);
  const script = scripts.dev ? "dev" : scripts.preview ? "preview" : "";
  if (!script) {
    return null;
  }

  const host = parsedUrl.hostname === "localhost" ? "127.0.0.1" : parsedUrl.hostname;
  const port = parsedUrl.port || (parsedUrl.protocol === "https:" ? "443" : "80");
  return {
    command: process.platform === "win32" ? "npm.cmd" : "npm",
    args: ["run", script, "--", "--host", host, "--port", port, "--strictPort"],
  };
}

async function ensurePreviewReachable(rawUrl, outputPath) {
  if (await urlResponds(rawUrl)) {
    return;
  }

  const parsedUrl = localHttpUrl(rawUrl);
  if (!parsedUrl) {
    return;
  }

  const projectRoot = projectRootFromOutput(outputPath);
  const command = previewCommand(projectRoot, parsedUrl);
  if (!command) {
    throw new Error(
      `Preview URL is not reachable: ${rawUrl}. No package.json dev or preview script was found.`,
    );
  }

  const child = spawn(command.command, command.args, {
    cwd: projectRoot,
    detached: true,
    stdio: "ignore",
    windowsHide: true,
  });
  child.unref();

  const startedAt = Date.now();
  while (Date.now() - startedAt < PREVIEW_START_TIMEOUT_MS) {
    if (await urlResponds(rawUrl)) {
      return;
    }
    if (child.exitCode !== null) {
      throw new Error(
        `Preview URL did not become reachable: ${rawUrl}. ` +
          `${command.command} ${command.args.join(" ")} exited with code ${child.exitCode}.`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  throw new Error(
    `Preview URL did not become reachable within ${PREVIEW_START_TIMEOUT_MS / 1000}s: ${rawUrl}. ` +
      `Started command: ${command.command} ${command.args.join(" ")}`,
  );
}

async function main() {
  const [, , url, outputPath] = process.argv;
  if (!url || !outputPath) {
    console.error("Usage: node capture_preview.js <url> <outputPath>");
    process.exit(2);
  }

  const executablePath =
    process.env.CHROME_PATH ||
    "C:/Program Files/Google/Chrome/Application/chrome.exe";

  const browser = await chromium.launch({
    headless: true,
    executablePath,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  await ensurePreviewReachable(url, outputPath);
  await page.goto(url, { waitUntil: "networkidle", timeout: 45000 });
  await page.screenshot({ path: outputPath, fullPage: true });
  await browser.close();
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
