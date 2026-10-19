// Claude Code usage display - LILYGO T-Display V1.1
//
// Two modes; a short press of the right button (GPIO35) toggles between them, and the icon
// left of the clock (top right) shows the current mode. Defaults to USB mode, then remembers
// the last mode (stored in NVS). The left button (GPIO0) toggles the backlight.
// Hold the right button for 5 s: erase the WiFi settings and open the WiFi setup page (QR code).
//
// USB mode: WiFi off; receives one JSON line over USB from usage_bridge.py:
//   {"s":2,"sr":16218,"w":68,"wr":231618,"st":"allowed","t":9582}
//
// WiFi mode: every 60 s the board itself sends a tiny 1-token request to the Anthropic API and
// reads only the rate-limit headers. USB is just power. WiFi and token are stored in NVS.
// Without WiFi settings the board opens its own hotspot and shows a QR code: scan it with a
// phone to join the hotspot, the setup page (http://192.168.4.1) pops up, pick a network and
// enter its password. provision.py can be used instead.
//
// Serial commands (one JSON line, accepted in both modes):
//   {"cfg":{"ssid":"..","pass":"..","token":".."}}  save settings (any subset), then reboot
//   {"cmd":"status"}                                  print current status (without the token)

#include <Arduino.h>
#include <ArduinoJson.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <qrcode.h>   // esp_qrcode built into ESP-IDF
#include <time.h>

#include "root_ca.h"

static const char *TZ_INFO = "CST-8";      // Taiwan time (UTC+8)
static const int BTN_LEFT = 0;     // toggle backlight
static const int BTN_RIGHT = 35;   // toggle USB / WiFi mode
static const uint32_t POLL_MS = 60000;     // query interval; every query is a real request
static const uint32_t RETRY_MS = 15000;    // retry delay after a failure
static const uint32_t STALE_MS = 180000;   // no new data for 3 minutes = stale
static const uint32_t LONG_PRESS_MS = 5000;  // right-button hold time to reset WiFi
static const uint32_t HOLD_HINT_MS = 700;    // show the countdown only after holding this long

static const char *API_URL = "https://api.anthropic.com/v1/messages";
static const char *API_BODY =
    "{\"model\":\"claude-haiku-4-5\",\"max_tokens\":1,"
    "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}";

enum Mode : uint8_t { MODE_USB = 0, MODE_WIFI = 1 };

TFT_eSPI tft;
TFT_eSprite canvas(&tft);   // draw in memory first, then push once to avoid flicker
Preferences prefs;
WebServer server(80);
DNSServer dns;

static uint16_t C_BG, C_FG, C_DIM, C_ACCENT, C_TRACK, C_OK, C_WARN, C_BAD;

struct Config {
  String ssid, pass, token;
} cfg;

struct Usage {
  bool valid = false;
  int s = 0, w = 0;
  long sr = 0, wr = 0;      // seconds until reset at time of receipt
  long clock = -1;          // time from the PC in USB mode (seconds since midnight)
  char st[24] = "";
  uint32_t rxMs = 0;        // millis() when this data arrived
} usage;

Mode mode = MODE_USB;
char errMsg[48] = "";
int lastHttp = 0;
char lineBuf[512];
size_t lineLen = 0;
bool fetching = false;
bool backlightOn = true;
uint32_t nextFetchMs = 0;
uint32_t restartAt = 0;      // delayed reboot after saving, so the response reaches the phone first

// WiFi setup page (hotspot + captive portal)
bool portalOn = false;
bool scanning = false;
char apSsid[24] = "";
char apPass[12] = "";
String netHtml;              // scanned WiFi list (HTML)
static const int QR_MAX = 41;            // QR version 6
uint8_t qrBits[QR_MAX * QR_MAX];
int qrSize = 0;

// Right button state (short / long press)
bool rDown = false, rLongDone = false;
uint32_t rDownMs = 0;

// ------------------------------------------------------------------------- //
//  Settings
// ------------------------------------------------------------------------- //
static void loadConfig() {
  prefs.begin("cfg", true);
  cfg.ssid = prefs.getString("ssid", "");
  cfg.pass = prefs.getString("pass", "");
  cfg.token = prefs.getString("token", "");
  mode = prefs.getUChar("mode", MODE_USB) == MODE_WIFI ? MODE_WIFI : MODE_USB;
  prefs.end();
}

static void saveConfig(JsonObject c) {
  prefs.begin("cfg", false);
  for (const char *k : {"ssid", "pass", "token"}) {
    if (c[k].is<const char *>()) prefs.putString(k, c[k].as<const char *>());
  }
  prefs.end();
}

static void saveMode() {
  prefs.begin("cfg", false);
  prefs.putUChar("mode", mode);
  prefs.end();
}

static bool timeValid() { return time(nullptr) > 1700000000; }
static bool wifiOnline() { return WiFi.status() == WL_CONNECTED; }

static void render();

// ------------------------------------------------------------------------- //
//  WiFi setup page: the board opens a hotspot and shows a QR code for joining it
// ------------------------------------------------------------------------- //
static void htmlEscape(String &out, const String &in) {
  for (char c : in) {
    switch (c) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += c;
    }
  }
}

// Scan nearby WiFi networks, sort by signal strength, drop duplicate names, build a tappable list
static void scanNetworks() {
  int n = WiFi.scanNetworks();
  int idx[30];
  int cnt = 0;
  for (int i = 0; i < n && cnt < 30; i++) {
    if (WiFi.SSID(i).isEmpty()) continue;
    idx[cnt++] = i;
  }
  for (int i = 0; i < cnt; i++)
    for (int j = i + 1; j < cnt; j++)
      if (WiFi.RSSI(idx[j]) > WiFi.RSSI(idx[i])) std::swap(idx[i], idx[j]);

  netHtml = "";
  int shown = 0;
  for (int i = 0; i < cnt && shown < 15; i++) {
    String ssid = WiFi.SSID(idx[i]);
    bool dup = false;
    for (int j = 0; j < i; j++) dup |= WiFi.SSID(idx[j]) == ssid;
    if (dup) continue;
    String esc;
    htmlEscape(esc, ssid);
    netHtml += "<div class=n data-s=\"" + esc + "\" onclick=\"pick(this)\">" + esc;
    netHtml += "<small>" + String(WiFi.RSSI(idx[i])) + " dBm" +
               (WiFi.encryptionType(idx[i]) == WIFI_AUTH_OPEN ? " open" : "") +
               "</small></div>";
    shown++;
  }
  if (!shown) netHtml = "<p><small>No WiFi found, rescan or type the name below</small></p>";
  WiFi.scanDelete();
}

static void onQr(esp_qrcode_handle_t qr) {
  qrSize = min(esp_qrcode_get_size(qr), QR_MAX);
  for (int y = 0; y < qrSize; y++)
    for (int x = 0; x < qrSize; x++)
      qrBits[y * qrSize + x] = esp_qrcode_get_module(qr, x, y);
}

static const char PAGE_HEAD[] PROGMEM =
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Claude usage display WiFi setup</title><style>"
    "body{font-family:sans-serif;background:#14100e;color:#f2eae4;max-width:420px;margin:auto;padding:16px}"
    "h2{color:#ec7456;font-size:20px}label{display:block;margin-top:14px}"
    "input,button{width:100%;box-sizing:border-box;padding:11px;margin-top:6px;font-size:16px;"
    "border-radius:8px;border:1px solid #3a302c;background:#221b18;color:#f2eae4}"
    "button{background:#ec7456;color:#14100e;border:0;font-weight:bold;margin-top:20px}"
    ".n{padding:11px;border-bottom:1px solid #3a302c;cursor:pointer}"
    ".n small{float:right}small{color:#9a8f88}a{color:#ec7456}"
    "</style></head><body>";

static void handleRoot() {
  if (server.hasArg("scan")) scanNetworks();
  String h;
  h.reserve(3000 + netHtml.length());
  h += FPSTR(PAGE_HEAD);
  h += F("<h2>Claude Code usage display - WiFi setup</h2>"
         "<div>Tap the WiFi network to connect to (2.4GHz only):</div>");
  h += netHtml;
  h += F("<p><a href='/?scan=1'>Rescan</a></p>"
         "<form method=post action=/save>"
         "<label>WiFi name<input id=s name=ssid maxlength=32 required autocomplete=off></label>"
         "<label>WiFi password<input id=p name=pass type=password maxlength=63 autocomplete=off></label>"
         "<label><input type=checkbox style='width:auto' "
         "onclick=\"p.type=this.checked?'text':'password'\"> Show password</label>"
         "<label>Claude token (optional)<input name=token type=password autocomplete=off "
         "placeholder='token from claude setup-token'></label><small>");
  h += cfg.token.isEmpty() ? F("The board has no token yet; WiFi mode needs one to query usage")
                           : F("The board already has a token; leave empty to keep it");
  h += F("</small><button>Save and connect</button></form>"
         "<script>function pick(e){s.value=e.dataset.s;p.focus()}</script></body></html>");
  server.send(200, "text/html; charset=utf-8", h);
}

static void handleSave() {
  String ssid = server.arg("ssid"), pass = server.arg("pass"), token = server.arg("token");
  ssid.trim();
  token.trim();
  const char *bad = ssid.isEmpty() || ssid.length() > 32 ? "Invalid WiFi name length"
                    : pass.length() && (pass.length() < 8 || pass.length() > 63)
                        ? "WiFi password must be 8 to 63 characters"
                        : nullptr;
  String h = FPSTR(PAGE_HEAD);
  if (bad) {
    h += "<p>" + String(bad) + "</p><p><a href='/'>Back</a></p></body></html>";
    server.send(400, "text/html; charset=utf-8", h);
    return;
  }
  prefs.begin("cfg", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  if (token.length()) prefs.putString("token", token);
  prefs.putUChar("mode", MODE_WIFI);
  prefs.end();
  String esc;
  htmlEscape(esc, ssid);
  h += "<h2>Saved</h2><p>The board is restarting and connecting to \"" + esc + "\".</p>"
       "<p>You can switch your phone back to your usual WiFi. If the board keeps showing Connecting, "
       "hold the right button for 5 seconds to set it up again.</p></body></html>";
  server.send(200, "text/html; charset=utf-8", h);
  restartAt = millis() + 1500;
}

// Phones' connectivity checks after joining the hotspot (generate_204, hotspot-detect...) are all
// redirected to the setup page, so the phone pops it up automatically
static void handleRedirect() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}

static void startPortal() {
  portalOn = true;
  scanning = true;
  render();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_AP_STA);
  scanNetworks();   // scan before starting the hotspot so the phone isn't dropped during a scan

  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(apSsid, sizeof apSsid, "ClaudeUsage-%02X%02X", mac[4], mac[5]);
  // Random hotspot password each time (avoiding look-alike characters); the QR code contains it
  const char *chars = "abcdefghjkmnpqrstuvwxyz23456789";
  for (int i = 0; i < 8; i++) apPass[i] = chars[esp_random() % strlen(chars)];
  apPass[8] = 0;
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1),
                    IPAddress(255, 255, 255, 0));
  WiFi.softAP(apSsid, apPass);
  dns.start(53, "*", WiFi.softAPIP());
  server.begin();

  char text[64];
  snprintf(text, sizeof text, "WIFI:T:WPA;S:%s;P:%s;;", apSsid, apPass);
  esp_qrcode_config_t qc = ESP_QRCODE_CONFIG_DEFAULT();
  qc.display_func = onQr;
  qc.max_qrcode_version = 6;
  qc.qrcode_ecc_level = ESP_QRCODE_ECC_MED;
  qrSize = 0;
  esp_qrcode_generate(&qc, text);
  scanning = false;
}

static void stopPortal() {
  if (!portalOn) return;
  server.stop();
  dns.stop();
  WiFi.softAPdisconnect(true);
  portalOn = false;
}

// ------------------------------------------------------------------------- //
//  Mode switching
// ------------------------------------------------------------------------- //
static void startMode() {
  stopPortal();
  if (mode == MODE_WIFI) {
    if (cfg.ssid.length()) {
      WiFi.mode(WIFI_STA);
      WiFi.setAutoReconnect(true);
      WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
      configTzTime(TZ_INFO, "pool.ntp.org", "time.google.com");
    } else {
      startPortal();
    }
    nextFetchMs = millis();
  } else {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    Serial.println("ready");   // tells usage_bridge.py to resend the last data immediately
  }
}

static void switchMode(Mode m) {
  mode = m;
  saveMode();
  // New data source: old data won't update anymore, clear it to avoid confusion
  usage = Usage();
  errMsg[0] = 0;
  lastHttp = 0;
  startMode();
}

static void toggleMode() { switchMode(mode == MODE_USB ? MODE_WIFI : MODE_USB); }

// Long press of the right button: erase WiFi credentials (keep the token), open the setup page
static void resetWifi() {
  prefs.begin("cfg", false);
  prefs.remove("ssid");
  prefs.remove("pass");
  prefs.end();
  cfg.ssid = "";
  cfg.pass = "";
  WiFi.disconnect(true);
  switchMode(MODE_WIFI);
}

// ------------------------------------------------------------------------- //
//  Query the API (WiFi mode)
// ------------------------------------------------------------------------- //
static int pctOf(const String &v) { return (int)lroundf(v.toFloat() * 100); }

static long remainOf(const String &v) {
  long r = (long)(atoll(v.c_str()) - (long long)time(nullptr));
  return r > 0 ? r : 0;
}

static bool fetchUsage() {
  WiFiClientSecure client;
  client.setCACert(ROOT_CA);
  HTTPClient http;
  http.setConnectTimeout(15000);
  http.setTimeout(20000);
  if (!http.begin(client, API_URL)) {
    strlcpy(errMsg, "HTTPS init failed", sizeof errMsg);
    return false;
  }
  http.setUserAgent("claude-code/2.1.5");
  http.addHeader("Authorization", "Bearer " + cfg.token);
  http.addHeader("anthropic-version", "2023-06-01");
  http.addHeader("anthropic-beta", "oauth-2025-04-20");
  http.addHeader("Content-Type", "application/json");
  const char *keys[] = {
      "anthropic-ratelimit-unified-5h-utilization",
      "anthropic-ratelimit-unified-5h-reset",
      "anthropic-ratelimit-unified-5h-status",
      "anthropic-ratelimit-unified-7d-utilization",
      "anthropic-ratelimit-unified-7d-reset",
      "anthropic-ratelimit-unified-overage-utilization",
      "anthropic-ratelimit-unified-overage-reset",
      "anthropic-ratelimit-unified-status",
  };
  http.collectHeaders(keys, sizeof keys / sizeof keys[0]);

  int code = http.POST(API_BODY);
  lastHttp = code;
  if (code <= 0) {
    snprintf(errMsg, sizeof errMsg, "Net: %s", http.errorToString(code).c_str());
    http.end();
    return false;
  }

  // 429 and other error responses carry the rate-limit headers too, so check headers before status
  bool ok = true;
  if (http.header(keys[0]).length()) {
    usage.s = pctOf(http.header(keys[0]));
    usage.sr = remainOf(http.header(keys[1]));
    usage.w = pctOf(http.header(keys[3]));
    usage.wr = remainOf(http.header(keys[4]));
    strlcpy(usage.st, http.header(keys[2]).c_str(), sizeof usage.st);
  } else if (http.header(keys[5]).length()) {
    usage.s = pctOf(http.header(keys[5]));
    usage.sr = remainOf(http.header(keys[6]));
    usage.w = -1;   // no weekly limit
    usage.wr = 0;
    strlcpy(usage.st, http.header(keys[7]).c_str(), sizeof usage.st);
  } else {
    ok = false;
    if (code == 401 || code == 403)
      snprintf(errMsg, sizeof errMsg, "Token rejected (HTTP %d)", code);
    else
      snprintf(errMsg, sizeof errMsg, "HTTP %d, no usage data", code);
    // The response body contains no token; print it for debugging
    Serial.printf("HTTP %d: %s\n", code, http.getString().substring(0, 300).c_str());
  }
  http.end();

  if (ok) {
    usage.clock = -1;
    usage.rxMs = millis();
    usage.valid = true;
    errMsg[0] = 0;
  }
  return ok;
}

// ------------------------------------------------------------------------- //
//  Display
// ------------------------------------------------------------------------- //
static long elapsedS() { return (long)((millis() - usage.rxMs) / 1000); }
static bool stale() { return !usage.valid || millis() - usage.rxMs > STALE_MS; }

static void fmtRemain(long secs, char *out, size_t n) {
  if (secs <= 0) { snprintf(out, n, "now"); return; }
  long m = (secs + 59) / 60;
  if (m >= 24 * 60) snprintf(out, n, "%ldd %ldh", m / 1440, (m % 1440) / 60);
  else if (m >= 60) snprintf(out, n, "%ldh %02ldm", m / 60, m % 60);
  else snprintf(out, n, "%ldm", m);
}

static uint16_t pctColor(int p) {
  return p >= 90 ? C_BAD : (p >= 70 ? C_WARN : C_OK);
}

static bool clockText(char *out, size_t n) {
  if (timeValid()) {
    time_t now = time(nullptr);
    struct tm lt;
    localtime_r(&now, &lt);
    snprintf(out, n, "%02d:%02d", lt.tm_hour, lt.tm_min);
    return true;
  }
  if (usage.valid && usage.clock >= 0) {
    long c = (usage.clock + elapsedS()) % 86400;
    snprintf(out, n, "%02ld:%02ld", c / 3600, (c / 60) % 60);
    return true;
  }
  return false;
}

// Icons are drawn inside an 18x16 box, (x, y) = top-left corner
static const int ICON_W = 18;

static void drawWifiIcon(int x, int y, uint16_t col) {
  int cx = x + ICON_W / 2, cy = y + 14;
  // drawArc angles start at 6 o'clock and go clockwise, 180 = straight up
  canvas.drawArc(cx, cy, 13, 11, 135, 225, col, C_BG, true);
  canvas.drawArc(cx, cy, 9, 7, 135, 225, col, C_BG, true);
  canvas.drawArc(cx, cy, 5, 3, 135, 225, col, C_BG, true);
  canvas.fillCircle(cx, cy - 1, 1, col);
}

static void drawUsbIcon(int x, int y, uint16_t col) {
  // Front of a USB-C port: capsule outline (2px) + the tongue in the middle
  canvas.fillRoundRect(x, y + 3, ICON_W, 11, 5, col);
  canvas.fillRoundRect(x + 2, y + 5, ICON_W - 4, 7, 3, C_BG);
  canvas.fillRoundRect(x + 5, y + 7, ICON_W - 10, 3, 1, col);
}

// One block: label, reset countdown, large percentage on the right, progress bar
static void drawBlock(int y, const char *label, int pct, long remain) {
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(C_FG, C_BG);
  canvas.drawString(label, 4, y, 2);

  char buf[24], txt[32];
  canvas.setTextColor(C_DIM, C_BG);
  if (pct < 0) {
    canvas.drawString("no limit", 4, y + 16, 2);
  } else {
    fmtRemain(remain, buf, sizeof buf);
    snprintf(txt, sizeof txt, "reset %s", buf);
    canvas.drawString(txt, 4, y + 16, 2);
  }

  canvas.setTextDatum(TR_DATUM);
  if (pct < 0) {
    canvas.setTextColor(C_DIM, C_BG);
    canvas.drawString("n/a", 236, y + 4, 4);
  } else {
    canvas.setTextColor(pctColor(pct), C_BG);
    snprintf(txt, sizeof txt, "%d%%", pct);
    canvas.drawString(txt, 236, y + 4, 4);
  }

  const int bx = 4, by = y + 34, bw = 232, bh = 10;
  canvas.fillRoundRect(bx, by, bw, bh, 4, C_TRACK);
  if (pct > 0) {
    int fw = bw * min(pct, 100) / 100;
    canvas.fillRoundRect(bx, by, max(fw, 8), bh, 4, pctColor(pct));
  }
}

// Before any data arrives, show which step we are stuck at
static void drawWaiting() {
  const char *l1, *l2 = "";
  uint16_t col = C_FG;
  if (mode == MODE_USB) {
    if (errMsg[0]) {
      l1 = errMsg;
      col = C_BAD;
    } else {
      l1 = "Waiting for PC...";
    }
    l2 = "run usage_bridge.py";
  } else if (!wifiOnline()) {
    l1 = "Connecting WiFi...";
    l2 = cfg.ssid.c_str();
  } else if (cfg.token.isEmpty()) {
    l1 = "WiFi OK - token missing";
    l2 = "run provision.py --token";
    col = C_WARN;
  } else if (errMsg[0]) {
    l1 = errMsg;
    l2 = "retrying...";
    col = C_BAD;
  } else if (!timeValid()) {
    l1 = "Syncing time...";
  } else {
    l1 = "Loading...";
  }
  canvas.setTextDatum(MC_DATUM);
  canvas.setTextColor(col, C_BG);
  canvas.drawString(l1, 120, 56, 2);
  canvas.setTextColor(C_DIM, C_BG);
  canvas.drawString(l2, 120, 80, 2);
  canvas.drawString(mode == MODE_USB ? "right: WiFi mode | hold 5s: WiFi setup"
                                     : "right: USB mode | hold 5s: reset WiFi",
                    120, 118, 1);
}

static void drawStatusLine() {
  char line[56];
  uint16_t col = C_DIM;
  long el = elapsedS();
  if (fetching) {
    snprintf(line, sizeof line, "refreshing...");
  } else if (strcmp(usage.st, "rejected") == 0) {
    snprintf(line, sizeof line, "LIMIT REACHED");
    col = C_BAD;
  } else if (stale()) {
    const char *why = errMsg[0] ? errMsg
                    : mode == MODE_USB ? "PC offline"
                    : !wifiOnline() ? "WiFi lost" : "no update";
    snprintf(line, sizeof line, "%s - data %ldm old", why, el / 60);
    col = C_BAD;
  } else if (strcmp(usage.st, "allowed_warning") == 0) {
    snprintf(line, sizeof line, "approaching limit");
    col = C_WARN;
  } else {
    snprintf(line, sizeof line, "updated %lds ago", el);
  }
  canvas.setTextDatum(BL_DATUM);
  canvas.setTextColor(col, C_BG);
  canvas.drawString(line, 4, 135, 1);
}

static void drawHeader() {
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(C_ACCENT, C_BG);
  canvas.drawString("Claude Code usage", 4, 1, 2);

  int right = 236;
  char hm[8];
  if (clockText(hm, sizeof hm)) {
    canvas.setTextDatum(TR_DATUM);
    canvas.setTextColor(C_DIM, C_BG);
    canvas.drawString(hm, right, 1, 2);
    right -= canvas.textWidth(hm, 2) + 6;
  }
  // Connected and data fresh -> bright; otherwise grey
  bool live = mode == MODE_WIFI ? wifiOnline() && !stale() : !stale();
  uint16_t col = live ? C_FG : C_DIM;
  if (mode == MODE_WIFI) drawWifiIcon(right - ICON_W, 1, col);
  else drawUsbIcon(right - ICON_W, 1, col);

  canvas.drawFastHLine(0, 19, 240, C_TRACK);
}

// Setup screen: QR code (join the hotspot) on the left, instructions on the right
static void drawPortal() {
  if (scanning || !qrSize) {
    canvas.setTextDatum(MC_DATUM);
    canvas.setTextColor(C_FG, C_BG);
    canvas.drawString("Scanning WiFi...", 120, 60, 2);
    return;
  }
  int sc = max(1, 131 / (qrSize + 4));       // 2-module quiet zone on every side
  int box = (qrSize + 4) * sc;
  int x0 = 2, y0 = (135 - box) / 2;
  canvas.fillRect(x0, y0, box, box, TFT_WHITE);
  for (int y = 0; y < qrSize; y++)
    for (int x = 0; x < qrSize; x++)
      if (qrBits[y * qrSize + x])
        canvas.fillRect(x0 + (x + 2) * sc, y0 + (y + 2) * sc, sc, sc, TFT_BLACK);

  int tx = x0 + box + 5;
  canvas.setTextDatum(TL_DATUM);
  canvas.setTextColor(C_ACCENT, C_BG);
  canvas.drawString("WiFi setup", tx, 2, 2);
  bool joined = WiFi.softAPgetStationNum() > 0;
  canvas.setTextColor(joined ? C_OK : C_FG, C_BG);
  canvas.drawString(joined ? "Phone connected" : "Scan with phone", tx, 24, 1);
  canvas.setTextColor(C_DIM, C_BG);
  canvas.drawString("WiFi name", tx, 40, 1);
  canvas.drawString("Password", tx, 64, 1);
  canvas.drawString("Then open", tx, 88, 1);
  canvas.setTextColor(C_FG, C_BG);
  canvas.drawString(apSsid, tx, 50, 1);
  canvas.drawString(apPass, tx, 74, 1);
  canvas.drawString("192.168.4.1", tx, 98, 1);
  canvas.setTextColor(C_DIM, C_BG);
  canvas.drawString("right: USB mode", tx, 124, 1);
}

// While the right button is held, show "keep holding to reset WiFi" at the bottom
static void drawHoldHint() {
  uint32_t held = millis() - rDownMs;
  if (!rDown || rLongDone || held < HOLD_HINT_MS) return;
  canvas.fillRect(0, 105, 240, 30, C_BG);
  canvas.drawFastHLine(0, 105, 240, C_TRACK);
  char txt[40];
  snprintf(txt, sizeof txt, "Hold to reset WiFi... %lus",
           (unsigned long)((LONG_PRESS_MS - held + 999) / 1000));
  canvas.setTextDatum(TC_DATUM);
  canvas.setTextColor(C_WARN, C_BG);
  canvas.drawString(txt, 120, 109, 1);
  canvas.fillRoundRect(20, 122, 200, 8, 3, C_TRACK);
  canvas.fillRoundRect(20, 122, max(8, (int)(200 * held / LONG_PRESS_MS)), 8, 3, C_WARN);
}

static void render() {
  canvas.fillSprite(C_BG);
  if (portalOn) {
    drawPortal();
    drawHoldHint();
    canvas.pushSprite(0, 0);
    return;
  }
  drawHeader();
  if (!usage.valid) {
    drawWaiting();
  } else {
    long el = elapsedS();
    drawBlock(23, "5h session", usage.s, usage.sr - el);
    drawBlock(77, "Weekly", usage.w, usage.wr - el);
    drawStatusLine();
  }
  drawHoldHint();
  canvas.pushSprite(0, 0);
}

// ------------------------------------------------------------------------- //
//  Serial
// ------------------------------------------------------------------------- //
static void printStatus() {
  JsonDocument d;
  d["mode"] = mode == MODE_WIFI ? "wifi" : "usb";
  d["ssid"] = cfg.ssid;
  d["portal"] = portalOn;
  d["wifi_status"] = (int)WiFi.status();   // 3 = connected, 1 = SSID not found, 4 = connect failed
  d["ip"] = WiFi.localIP().toString();
  d["rssi"] = WiFi.RSSI();
  d["token_set"] = !cfg.token.isEmpty();
  d["time_ok"] = timeValid();
  d["last_http"] = lastHttp;
  d["err"] = errMsg;
  d["data"] = usage.valid;
  if (usage.valid) {
    d["s"] = usage.s;
    d["w"] = usage.w;
    d["age_s"] = elapsedS();
  }
  serializeJson(d, Serial);
  Serial.println();
}

static void handleLine(const char *line) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;

  if (doc["cfg"].is<JsonObject>()) {
    saveConfig(doc["cfg"].as<JsonObject>());
    Serial.println("cfg ok");
    Serial.flush();
    delay(200);
    ESP.restart();
  }
  if (doc["cmd"] == "status") {
    printStatus();
    return;
  }
  // The rest is data from usage_bridge.py, used only in USB mode
  if (mode != MODE_USB) return;
  if (doc["err"].is<const char *>()) {
    strlcpy(errMsg, doc["err"] | "", sizeof errMsg);
    return;
  }
  if (!doc["s"].is<int>()) return;
  usage.s = doc["s"];
  usage.w = doc["w"] | -1;
  usage.sr = doc["sr"] | 0L;
  usage.wr = doc["wr"] | 0L;
  usage.clock = doc["t"] | -1L;
  strlcpy(usage.st, doc["st"] | "", sizeof usage.st);
  usage.rxMs = millis();
  usage.valid = true;
  errMsg[0] = 0;
}

static void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = 0;
      handleLine(lineBuf);
      lineLen = 0;
    } else if (c != '\r' && lineLen < sizeof lineBuf - 1) {
      lineBuf[lineLen++] = c;
    }
  }
}

// Right button: short press counts on release (returns 1), holding 5 s fires a long press at once (returns 2)
static int rightButton() {
  bool down = digitalRead(BTN_RIGHT) == LOW;
  uint32_t now = millis();
  int ev = 0;
  if (down && !rDown) {
    rDownMs = now;
    rLongDone = false;
  } else if (down && !rLongDone && now - rDownMs >= LONG_PRESS_MS) {
    rLongDone = true;
    ev = 2;
  } else if (!down && rDown && !rLongDone && now - rDownMs >= 30) {
    ev = 1;   // under 30ms counts as bounce
  }
  rDown = down;
  return ev;
}

// Fires once on press, with simple debouncing
static bool pressed(int pin, bool &prev, uint32_t &lastMs) {
  bool down = digitalRead(pin) == LOW;
  bool fire = down && !prev && millis() - lastMs > 200;
  if (down != prev) lastMs = millis();
  prev = down;
  return fire;
}

// ------------------------------------------------------------------------- //
void setup() {
  Serial.begin(115200);
  pinMode(BTN_LEFT, INPUT_PULLUP);
  pinMode(BTN_RIGHT, INPUT);   // GPIO35 has no internal pull-up; the board has an external one

  tft.init();
  tft.setRotation(1);       // landscape 240x135
  canvas.createSprite(240, 135);

  C_BG = tft.color565(0x14, 0x10, 0x0e);
  C_FG = tft.color565(0xf2, 0xea, 0xe4);
  C_DIM = tft.color565(0x9a, 0x8f, 0x88);
  C_ACCENT = tft.color565(0xec, 0x74, 0x56);
  C_TRACK = tft.color565(0x3a, 0x30, 0x2c);
  C_OK = tft.color565(0x3e, 0xcf, 0x8e);
  C_WARN = tft.color565(0xff, 0xb4, 0x54);
  C_BAD = tft.color565(0xff, 0x5f, 0x5f);

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleRedirect);

  loadConfig();
  startMode();
  render();
}

void loop() {
  static bool lPrev = false;
  static uint32_t lMs = 0, drawMs = 0;

  readSerial();
  if (portalOn) {
    dns.processNextRequest();
    server.handleClient();
  }
  if (restartAt && (int32_t)(millis() - restartAt) >= 0) ESP.restart();

  int ev = rightButton();
  if (ev == 1) toggleMode();
  else if (ev == 2) resetWifi();
  if (ev) drawMs = 0;
  if (pressed(BTN_LEFT, lPrev, lMs)) {
    backlightOn = !backlightOn;
    digitalWrite(TFT_BL, backlightOn ? TFT_BACKLIGHT_ON : !TFT_BACKLIGHT_ON);
  }

  // TLS certificate checks need the correct time, so start querying only after NTP sync
  if (mode == MODE_WIFI && wifiOnline() && !cfg.token.isEmpty() && timeValid() &&
      (int32_t)(millis() - nextFetchMs) >= 0) {
    fetching = true;
    render();
    bool ok = fetchUsage();
    fetching = false;
    nextFetchMs = millis() + (ok ? POLL_MS : RETRY_MS);
    drawMs = 0;
  }

  // Redraw faster while the right button is held so the countdown is smooth
  if (millis() - drawMs >= (rDown ? 100u : 500u)) {
    drawMs = millis();
    render();
  }
  delay(10);
}
