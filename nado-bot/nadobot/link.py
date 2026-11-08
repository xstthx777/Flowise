"""Локальная страница для подписи LinkSigner в MetaMask.

Открывается на http://127.0.0.1:8765 только на твоём компьютере.
MetaMask подписывает EIP-712 сообщение, подпись уходит обратно в этот процесс,
и он отправляет её в Nado. Приватный ключ MetaMask никуда не передаётся.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .exchange import Exchange

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Nado Bot — привязка</title>
<style>body{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px;background:#111;color:#eee}
button{font-size:18px;padding:12px 20px;border-radius:8px;border:0;background:#f6851b;color:#fff;cursor:pointer}
pre{background:#222;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-all;font-size:12px}
.ok{color:#5f5}.err{color:#f66}</style></head><body>
<h2>__TITLE__</h2>
<p>Кошелёк MetaMask: <b>__OWNER__</b><br>Бот-кошелёк: <b>__BOT__</b></p>
<p>MetaMask покажет запрос подписи <b>LinkSigner</b> (домен Nado). Это не транзакция и не тратит газ.
Проверь, что поле <code>signer</code> начинается с адреса бот-кошелька.</p>
<button id="b">Подписать в MetaMask</button>
<p id="s"></p>
<details><summary>Что подписываем</summary><pre id="td"></pre></details>
<script>
const TD = __TYPED__;
const OWNER = "__OWNER__".toLowerCase();
const CHAIN = "0x" + Number(TD.domain.chainId).toString(16);
document.getElementById('td').textContent = JSON.stringify(TD, null, 2);
const s = document.getElementById('s');
document.getElementById('b').onclick = async () => {
  try {
    if (!window.ethereum) throw new Error('MetaMask не найден в этом браузере');
    const [acc] = await ethereum.request({method:'eth_requestAccounts'});
    if (acc.toLowerCase() !== OWNER) throw new Error('В MetaMask выбран ' + acc + ', а в .env указан ' + OWNER);
    try { await ethereum.request({method:'wallet_switchEthereumChain', params:[{chainId: CHAIN}]}); }
    catch (e) {
      if (e.code === 4902) await ethereum.request({method:'wallet_addEthereumChain', params:[__CHAIN_PARAMS__]});
      else throw e;
    }
    s.textContent = 'Жду подпись...';
    const sig = await ethereum.request({method:'eth_signTypedData_v4', params:[acc, JSON.stringify(TD)]});
    const r = await fetch('/signature', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({signature: sig})});
    const j = await r.json();
    s.className = j.ok ? 'ok' : 'err';
    s.textContent = j.message;
  } catch (e) { s.className = 'err'; s.textContent = 'Ошибка: ' + (e.message || e); }
};
</script></body></html>"""

CHAINS = {
    57073: {"chainId": "0xdef1", "chainName": "Ink", "rpcUrls": ["https://rpc-gel.inkonchain.com"],
            "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
            "blockExplorerUrls": ["https://explorer.inkonchain.com"]},
    763373: {"chainId": "0xba5ed", "chainName": "Ink Sepolia",
             "rpcUrls": ["https://rpc-gel-sepolia.inkonchain.com"],
             "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
             "blockExplorerUrls": ["https://explorer-sepolia.inkonchain.com"]},
}


def _jsonable(o):
    if isinstance(o, bytes):
        return "0x" + o.hex()
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    return o


def run_link_server(ex: Exchange, unlink: bool = False, port: int = 8765) -> bool:
    typed = _jsonable(ex.link_signer_typed_data(unlink=unlink))
    chain_id = int(typed["domain"]["chainId"])
    chain_params = CHAINS.get(chain_id, {"chainId": hex(chain_id)})
    title = "Отвязка бот-ключа" if unlink else "Привязка бот-ключа к Nado"
    html = (PAGE.replace("__TYPED__", json.dumps(typed))
            .replace("__CHAIN_PARAMS__", json.dumps(chain_params))
            .replace("__OWNER__", ex.owner)
            .replace("__BOT__", ex.bot.address)
            .replace("__TITLE__", title))
    result = {"ok": False}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self._send(200, html, "text/html")
            else:
                self._send(404, "{}")

        def do_POST(self):
            if self.path != "/signature":
                return self._send(404, "{}")
            n = int(self.headers.get("Content-Length", 0))
            try:
                sig = json.loads(self.rfile.read(n))["signature"]
                ex.submit_link_signer(typed["message"], sig)
                linked = ex.linked_signer()
                ok = (linked.lower().startswith("0x" + "0" * 40)) if unlink else ex.is_bot_linked()
                msg = (f"Готово. Linked signer сейчас: {linked}. Окно можно закрыть." if ok
                       else f"Nado принял запрос, но linked signer = {linked}. Проверь вручную.")
                result["ok"] = ok
            except Exception as e:  # noqa: BLE001
                msg = f"Nado отклонил подпись: {e}"
            self._send(200, json.dumps({"ok": result["ok"], "message": msg}))
            done.set()

    srv = HTTPServer(("127.0.0.1", port), H)  # только localhost
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Открой в браузере с MetaMask: {url}")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    done.wait()
    srv.shutdown()
    return result["ok"]
