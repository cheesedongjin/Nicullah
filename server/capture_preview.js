const os = require("os");
const path = require("path");

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
  await page.goto(url, { waitUntil: "networkidle", timeout: 45000 });
  await page.screenshot({ path: outputPath, fullPage: true });
  await browser.close();
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
