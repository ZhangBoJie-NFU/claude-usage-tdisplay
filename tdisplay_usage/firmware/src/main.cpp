// Claude Code 用量顯示 - LILYGO T-Display V1.1
//
// 兩種模式，右鍵 (GPIO35) 短按互相切換，右上角時鐘左邊顯示目前模式的圖示。
// 出廠預設 USB 模式，之後記住最後選的模式（存在 NVS）。左鍵 (GPIO0) 開關背光。
// 右鍵長按 5 秒：清除 WiFi 設定，進入 WiFi 設定頁（QR code）。
//
// USB 模式：WiFi 關閉，接收 usage_bridge.py 從 USB 送來的一行 JSON：
//   {"s":2,"sr":16218,"w":68,"wr":231618,"st":"allowed","t":9582}
//
// WiFi 模式：板子自己每 60 秒向 Anthropic API 送出 1 token 的極小請求，只讀回應中的
// rate-limit 標頭。USB 只需供電。WiFi 與 token 存在 NVS。
// 還沒設定 WiFi 時，板子自己開一個熱點並顯示 QR code：手機掃描加入熱點後會跳出
// 設定頁 (http://192.168.4.1)，選 WiFi、輸入密碼即可。也可以用 provision.py 設定。
//
// 序列埠指令（一行 JSON，兩種模式都接受）：
//   {"cfg":{"ssid":"..","pass":"..","token":".."}}  寫入設定（可只給部分欄位）後重開機
//   {"cmd":"status"}                                  印出目前狀態（不含 token）

#include <Arduino.h>
#include <ArduinoJson.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <qrcode.h>   // ESP-IDF 內建的 esp_qrcode
#include <time.h>

#include "root_ca.h"

static const char *TZ_INFO = "CST-8";      // 台灣時間 (UTC+8)
static const int BTN_LEFT = 0;     // 開關背光
static const int BTN_RIGHT = 35;   // 切換 USB / WiFi 模式
static const uint32_t POLL_MS = 60000;     // 查詢間隔；每次都是一個真實請求
static const uint32_t RETRY_MS = 15000;    // 失敗後多久重試
static const uint32_t STALE_MS = 180000;   // 超過 3 分鐘沒有新資料 = 資料過期
static const uint32_t LONG_PRESS_MS = 5000;  // 右鍵長按多久重設 WiFi
static const uint32_t HOLD_HINT_MS = 700;    // 按住超過這個時間才顯示倒數

static const char *API_URL = "https://api.anthropic.com/v1/messages";
static const char *API_BODY =
    "{\"model\":\"claude-haiku-4-5\",\"max_tokens\":1,"
    "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}";

enum Mode : uint8_t { MODE_USB = 0, MODE_WIFI = 1 };

TFT_eSPI tft;
TFT_eSprite canvas(&tft);   // 先畫在記憶體再一次送出，避免閃爍
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
  long sr = 0, wr = 0;      // 收到時距離重置的秒數
  long clock = -1;          // USB 模式下 PC 給的時間（午夜起算秒數）
  char st[24] = "";
  uint32_t rxMs = 0;        // 收到這筆資料時的 millis()
} usage;

Mode mode = MODE_USB;
char errMsg[48] = "";
int lastHttp = 0;
char lineBuf[512];
size_t lineLen = 0;
bool fetching = false;
bool backlightOn = true;
uint32_t nextFetchMs = 0;
uint32_t restartAt = 0;      // 設定頁存檔後延遲重開機，讓回應先送到手機

// WiFi 設定頁（熱點 + captive portal）
bool portalOn = false;
bool scanning = false;
char apSsid[24] = "";
char apPass[12] = "";
String netHtml;              // 掃描到的 WiFi 清單（HTML）
static const int QR_MAX = 41;            // QR version 6
uint8_t qrBits[QR_MAX * QR_MAX];
int qrSize = 0;

// 右鍵狀態（短按 / 長按）
bool rDown = false, rLongDone = false;
uint32_t rDownMs = 0;

// ------------------------------------------------------------------------- //
//  設定
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
//  WiFi 設定頁：板子開熱點，畫面顯示加入熱點用的 QR code
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

// 掃描附近的 WiFi，依訊號強度排序、去掉重複名稱，做成可點選的清單
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
               (WiFi.encryptionType(idx[i]) == WIFI_AUTH_OPEN ? " 開放" : "") +
               "</small></div>";
    shown++;
  }
  if (!shown) netHtml = "<p><small>找不到 WiFi，請重新掃描或直接輸入名稱</small></p>";
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
    "<title>Claude 用量顯示 WiFi 設定</title><style>"
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
  h += F("<h2>Claude Code 用量顯示 - WiFi 設定</h2>"
         "<div>點選要連接的 WiFi（只支援 2.4GHz）：</div>");
  h += netHtml;
  h += F("<p><a href='/?scan=1'>重新掃描</a></p>"
         "<form method=post action=/save>"
         "<label>WiFi 名稱<input id=s name=ssid maxlength=32 required autocomplete=off></label>"
         "<label>WiFi 密碼<input id=p name=pass type=password maxlength=63 autocomplete=off></label>"
         "<label><input type=checkbox style='width:auto' "
         "onclick=\"p.type=this.checked?'text':'password'\"> 顯示密碼</label>"
         "<label>Claude token（選填）<input name=token type=password autocomplete=off "
         "placeholder='claude setup-token 產生的 token'></label><small>");
  h += cfg.token.isEmpty() ? F("板子還沒有 token，WiFi 模式需要它才能查詢用量")
                           : F("板子已有 token，留空表示保持不變");
  h += F("</small><button>儲存並連線</button></form>"
         "<script>function pick(e){s.value=e.dataset.s;p.focus()}</script></body></html>");
  server.send(200, "text/html; charset=utf-8", h);
}

static void handleSave() {
  String ssid = server.arg("ssid"), pass = server.arg("pass"), token = server.arg("token");
  ssid.trim();
  token.trim();
  const char *bad = ssid.isEmpty() || ssid.length() > 32 ? "WiFi 名稱長度不正確"
                    : pass.length() && (pass.length() < 8 || pass.length() > 63)
                        ? "WiFi 密碼必須是 8 到 63 個字元"
                        : nullptr;
  String h = FPSTR(PAGE_HEAD);
  if (bad) {
    h += "<p>" + String(bad) + "</p><p><a href='/'>返回</a></p></body></html>";
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
  h += "<h2>已儲存</h2><p>板子正在重新啟動並連線到「" + esc + "」。</p>"
       "<p>手機可以切回原本的 WiFi 了。若板子一直顯示 Connecting，"
       "長按右鍵 5 秒重新設定。</p></body></html>";
  server.send(200, "text/html; charset=utf-8", h);
  restartAt = millis() + 1500;
}

// 手機連上熱點後的連網偵測（generate_204、hotspot-detect…）一律導向設定頁，
// 手機就會自動跳出設定畫面
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
  scanNetworks();   // 先掃描再開熱點，免得掃描時手機斷線

  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(apSsid, sizeof apSsid, "ClaudeUsage-%02X%02X", mac[4], mac[5]);
  // 每次隨機產生熱點密碼（避開容易看錯的字元），QR code 裡已含密碼
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
//  模式切換
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
    Serial.println("ready");   // 讓 usage_bridge.py 立刻重送最後一筆資料
  }
}

static void switchMode(Mode m) {
  mode = m;
  saveMode();
  // 換了資料來源，舊資料不再更新，清掉以免誤會
  usage = Usage();
  errMsg[0] = 0;
  lastHttp = 0;
  startMode();
}

static void toggleMode() { switchMode(mode == MODE_USB ? MODE_WIFI : MODE_USB); }

// 長按右鍵：清除 WiFi 帳密（token 保留），進入 WiFi 模式的設定頁
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
//  查詢 API（WiFi 模式）
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

  // 429 等錯誤回應同樣帶有 rate-limit 標頭，所以先看標頭再看狀態碼
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
    usage.w = -1;   // 沒有每週限制
    usage.wr = 0;
    strlcpy(usage.st, http.header(keys[7]).c_str(), sizeof usage.st);
  } else {
    ok = false;
    if (code == 401 || code == 403)
      snprintf(errMsg, sizeof errMsg, "Token rejected (HTTP %d)", code);
    else
      snprintf(errMsg, sizeof errMsg, "HTTP %d, no usage data", code);
    // 回應內容不含 token，印出來方便除錯
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
//  畫面
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

// 圖示都畫在 18x16 的框內，(x, y) 為左上角
static const int ICON_W = 18;

static void drawWifiIcon(int x, int y, uint16_t col) {
  int cx = x + ICON_W / 2, cy = y + 14;
  // drawArc 的角度從 6 點鐘方向順時針算，180 = 正上方
  canvas.drawArc(cx, cy, 13, 11, 135, 225, col, C_BG, true);
  canvas.drawArc(cx, cy, 9, 7, 135, 225, col, C_BG, true);
  canvas.drawArc(cx, cy, 5, 3, 135, 225, col, C_BG, true);
  canvas.fillCircle(cx, cy - 1, 1, col);
}

static void drawUsbIcon(int x, int y, uint16_t col) {
  // USB-C 接口正面：膠囊形外框（2px）+ 中間的舌片
  canvas.fillRoundRect(x, y + 3, ICON_W, 11, 5, col);
  canvas.fillRoundRect(x + 2, y + 5, ICON_W - 4, 7, 3, C_BG);
  canvas.fillRoundRect(x + 5, y + 7, ICON_W - 10, 3, 1, col);
}

// 一個區塊：標籤、重置倒數、右側大字百分比、進度條
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

// 還沒有資料時，中間顯示目前卡在哪一步
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
  // 連線正常且資料新鮮 -> 亮色；否則灰色
  bool live = mode == MODE_WIFI ? wifiOnline() && !stale() : !stale();
  uint16_t col = live ? C_FG : C_DIM;
  if (mode == MODE_WIFI) drawWifiIcon(right - ICON_W, 1, col);
  else drawUsbIcon(right - ICON_W, 1, col);

  canvas.drawFastHLine(0, 19, 240, C_TRACK);
}

// 設定頁畫面：左邊 QR code（加入熱點），右邊文字說明
static void drawPortal() {
  if (scanning || !qrSize) {
    canvas.setTextDatum(MC_DATUM);
    canvas.setTextColor(C_FG, C_BG);
    canvas.drawString("Scanning WiFi...", 120, 60, 2);
    return;
  }
  int sc = max(1, 131 / (qrSize + 4));       // 四周留 2 格白邊
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

// 按住右鍵時在底部顯示「再按幾秒就重設 WiFi」
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
//  序列埠
// ------------------------------------------------------------------------- //
static void printStatus() {
  JsonDocument d;
  d["mode"] = mode == MODE_WIFI ? "wifi" : "usb";
  d["ssid"] = cfg.ssid;
  d["portal"] = portalOn;
  d["wifi_status"] = (int)WiFi.status();   // 3 = 已連線，1 = 找不到 SSID，4 = 連線失敗
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
  // 以下是 usage_bridge.py 的資料，只在 USB 模式採用
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

// 右鍵：放開時才算短按（回傳 1），按住滿 5 秒立刻觸發長按（回傳 2）
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
    ev = 1;   // 少於 30ms 視為彈跳
  }
  rDown = down;
  return ev;
}

// 按下瞬間觸發一次，含簡單消抖
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
  pinMode(BTN_RIGHT, INPUT);   // GPIO35 沒有內部上拉，板上已有外部上拉

  tft.init();
  tft.setRotation(1);       // 橫向 240x135
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

  // TLS 憑證驗證需要正確時間，所以等 NTP 對時後才開始查詢
  if (mode == MODE_WIFI && wifiOnline() && !cfg.token.isEmpty() && timeValid() &&
      (int32_t)(millis() - nextFetchMs) >= 0) {
    fetching = true;
    render();
    bool ok = fetchUsage();
    fetching = false;
    nextFetchMs = millis() + (ok ? POLL_MS : RETRY_MS);
    drawMs = 0;
  }

  // 按住右鍵時畫面更新快一點，倒數才順
  if (millis() - drawMs >= (rDown ? 100u : 500u)) {
    drawMs = millis();
    render();
  }
  delay(10);
}
