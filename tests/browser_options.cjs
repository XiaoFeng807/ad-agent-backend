// Default to Playwright's versioned Chromium; use an installed channel explicitly.
function browserOptions() {
  const channel = process.env.AD_BROWSER_CHANNEL || "chromium";
  if (!["chromium", "chrome", "msedge"].includes(channel)) {
    throw new Error(`Unsupported AD_BROWSER_CHANNEL: ${channel}`);
  }
  return channel === "chromium" ? {} : { channel };
}
module.exports = { browserOptions };
