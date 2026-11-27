const SERVER_CONFIG_KEY = "tmallPrescriptionServerConfig";
const endpointInput = document.querySelector("#endpoint");
const tokenInput = document.querySelector("#token");
const saveButton = document.querySelector("#save");
const message = document.querySelector("#message");
const DEFAULT_ENDPOINT = "http://172.21.0.2:8095/internal/tmall/prescription-batches";

function parsedEndpoint(value) {
  const url = new URL(String(value || "").trim());
  if (!/^https?:$/.test(url.protocol) || url.username || url.password) {
    throw new Error("接收地址必须是无内嵌用户名密码的 HTTP(S) 地址");
  }
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  const trustedPrivateReceiver = url.protocol === "http:"
    && url.hostname === "172.21.0.2"
    && url.port === "8095";
  if (url.protocol !== "https:" && !loopback && !trustedPrivateReceiver) {
    throw new Error("HTTP 仅允许本机或指定的内部处方服务器");
  }
  return url;
}

chrome.storage.local.get(SERVER_CONFIG_KEY).then((data) => {
  const config = data[SERVER_CONFIG_KEY] || {};
  endpointInput.value = config.endpoint || DEFAULT_ENDPOINT;
  tokenInput.value = config.token || "";
});

saveButton.addEventListener("click", async () => {
  message.textContent = "";
  saveButton.disabled = true;
  try {
    const endpoint = parsedEndpoint(endpointInput.value);
    const token = tokenInput.value.trim();
    if (token.length < 32) throw new Error("接收令牌至少需要32位");
    const originPattern = `${endpoint.protocol}//${endpoint.host}/*`;
    const granted = await chrome.permissions.request({ origins: [originPattern] });
    if (!granted) throw new Error("未授予该服务器地址的访问权限");
    await chrome.storage.local.set({
      [SERVER_CONFIG_KEY]: { endpoint: endpoint.href, token },
    });
    message.textContent = "设置已保存。订单汇总完成后会自动发送服务器。";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
});
