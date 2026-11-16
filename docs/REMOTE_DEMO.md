# StorePilot 远程面试演示

GitHub README 和截图用于投递。面试官想亲手试用时，再给一个独立演示网址。浏览器访问云端的 FastAPI，和本地开发实例各用自己的数据目录和配置。演示账号内只提供合成商品、导入的公开历史数据和可重置的本地操作；测试商品的修改仍经过人工批准。

已在本机 Windows/Edge 验证销量、协作进度、人工批准及刷新读回。云部署和 Linux 运行待实测。 [工作台实拍](SHOWCASE.md)

## 1. 在云端启动独立实例

1. 准备一台持续在线的云服务器。把匹配验收的新版源码和锁文件部署到独立工作目录。展示分支主要提供文档与实拍，可运行的新版源码需单独准备。将 `DIAGNOSIS_SANDBOX_DB_PATH` 指向独立业务 SQLite 的绝对路径。Chroma、模型缓存和 `.env` 也使用云端实例自己的目录。若使用沙箱路径，先验证该范围内的广告审批配置和登记；面试演示可以保留广告提案在待审批状态。保留合成数据的初始快照，必要时停服后从该快照重置演示库。云端只导入需要展示的合成商品或公开历史数据。
2. 在服务端配置独立的 `JWT_SECRET`、演示用模型密钥和独立 `RAG_DB_PATH`。演示真实 GLM 时用 `LLM_PROVIDER=glm` 和 `GLM_API_KEY`。真实 RAG 使用锁定的 BGE 权重。Amazon 授权配置留空，GPU 重排保持关闭。 依赖使用随新版源码提供的 `locks/bge.txt`。
3. 仅向来宾提供独立普通账号，并在演示库停用或更换自动生成的默认账号凭据。重置演示库后也重新检查账号。默认账号在本地调试中可用，直接使用它们对外演示会让访问者获得超出演示需要的权限。
4. 单进程监听回环地址。下面是 Linux 手动预检命令。确认跑通后将应用与 `cloudflared` 注册为云端服务，由服务管理器维护进程和重启。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip --isolated install --require-hashes -r locks/bge.txt
.venv/bin/python -m uvicorn fast_server:app --host 127.0.0.1 --port 5010 --workers 1
```

## 2. 配置 HTTPS 和访客范围

在 Cloudflare 管理自己的域名，例如 `example.com`。为 `demo.example.com` 建立受 Access 保护的自托管应用，允许策略只纳入收件人的邮箱，并配置邮件一次性验证码。建立命名 Tunnel，在同一台云服务器运行 `cloudflared`，将该主机名代理到 `http://127.0.0.1:5010`。来宾通过浏览器访问 HTTPS 域名，完成邮箱验证码验证，再用演示账号登录项目。源站的 5010 仍只监听回环。 [Cloudflare 受保护的 Web 应用配置](https://developers.cloudflare.com/cloudflare-one/setup/secure-private-apps/private-web-app/)

演示的模型回复和工作进度使用 SSE。域名应走命名 Tunnel，`trycloudflare.com` Quick Tunnel 不支持 SSE。若部署路径还经过自建反向代理，关闭 SSE 路径的响应缓冲和缓存，以实际浏览器验收为准。 [Nginx 缓冲配置](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering) · [Quick Tunnel 限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

## 3. 从外网做一次走查再分享

- 用手机流量或另一条外网，在无痕窗口打开演示域名。验证未获准邮箱不能进入、允许邮箱能登录、页面资源和示例会话可读。
- 发一条销量问题，再发一条已验过的协作复核问题。浏览器应在任务结束前收到 `/api/chat/stream` 的首帧和进度，界面应随 SSE 显示取证与等待，最后显示完成。记录外网实测的首帧时间和一次完整运行时长。
- 用演示账号批准一项本地测试商品改动。浏览器批准后应另发 GET，读回实际版本和回执。刷新后重新打开原答案，应看到同一个回执。从另一演示会话核对记录未串入。
- 先把 GitHub 截图或录屏发给面试官。云端走查通过后再发演示域名和账号，面试结束后移除邮箱授权。
