# AgentRouter OpenAI Adapter

> 将 [AgentRouter.org](https://agentrouter.org) 的 Responses API 协议无缝转换为标准 OpenAI `chat/completions` 接口。
> 支持 **真流式（True SSE Streaming）**、**工具调用（Function Calling）** 与 **429 速率限制智能缓冲退避**。

---

## 🌟 核心痛点与特性

AgentRouter 免费提供的 `gpt-6.1-sol`、`gpt-6-astra`、`gpt-5.6-sol` 等高级模型采用的是 OpenAI 最新的 **Responses API**（`/v1/responses`）。市面上大多数现有客户端（如 NextChat、LobeChat、Cherry Studio、沉浸式翻译等）均无法直接连接使用。

本项目提供了一个轻量级、高并发的本地/云端中间件，解决以下问题：

1. **协议无缝桥接**：将标准 OpenAI `/v1/chat/completions` 与 `/v1/models` 自动翻译为 Responses API，客户端填入即可直接使用。
2. **毫秒级真流式（True SSE）**：上游每生成一个 Token 即刻流式输出，首字响应延迟从 10+ 秒大幅降至 1~1.5 秒。
3. **工具调用完整支持**：流式与非流式下均支持 Function Calling 双向转换，并解决上游断流时漏发结束帧的问题。
4. **429 智能重试与退避**：底层 Azure 节点有 60 秒滑动窗口 Token 限制（TPM），遇到 429 限流时自动在后台等待并重试，客户端平滑不弹红字报错。
5. **微节流防 WAF 突发**：智能体连续多轮快速交互时自动应用 2 秒请求微间隔，防止触发上游 WAF。
6. **多用户 Key 透传**：支持客户端自带 `Authorization: Bearer sk-...`，多用户调用各用各的额度；也支持服务端统一配置默认 Key。

---

## 🚀 快速开始

### 方式一：Docker 运行（推荐）

```bash
docker run -d \
  --name agentrouter-adapter \
  -p 8045:8045 \
  --restart unless-stopped \
  ghcr.io/zqhaqha/agentrouter-adapter:latest
```

*(如果尚未构建镜像，可通过本地克隆后构建)*：

```bash
git clone https://github.com/zqhaqha/agentrouter-adapter.git
cd agentrouter-adapter
docker compose up -d
```

### 方式二：Python 直接运行

需要 Python 3.9+ 环境：

```bash
git clone https://github.com/zqhaqha/agentrouter-adapter.git
cd agentrouter-adapter
pip install -r requirements.txt
python adapter.py
```

---

## ⚙️ 环境变量说明

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8045` | 服务监听端口 |
| `HOST` | `0.0.0.0` | 服务监听主机绑定 |
| `UPSTREAM_BASE` | `https://agentrouter.org` | AgentRouter 上游地址 |
| `AGENTROUTER_API_KEY` | *(空)* | 可选。如果客户端请求未携带 Key，将使用此默认 Key |

---

## 📱 客户端配置指南

启动本转接器后，在任何支持 OpenAI 格式的客户端中填写：

* **接口地址 (Base URL)**：`http://<你的服务器IP>:8045/v1`
* **API Key**：填入你在 AgentRouter.org 获取的 `sk-...`
* **支持的模型名称**：
  * `gpt-6.1-sol`（推荐主力，全天候供应）
  * `gpt-6-astra`
  * `gpt-5.6-sol`

### 常见客户端示例：
* **NextChat / ChatGPT-Next-Web**：
  * 接口地址：`http://127.0.0.1:8045`
  * 自定义模型：`+gpt-6.1-sol,+gpt-6-astra,+gpt-5.6-sol`
* **Cherry Studio / LobeChat**：
  * 添加自定义 OpenAI 提供商，Base URL 设置为 `http://127.0.0.1:8045/v1`
* **Hermes Agent**：
  * 在 `config.yaml` 或通过 `/model` 添加自定义 provider 指向 `http://127.0.0.1:8045/v1`

---

## 📄 开源许可

[MIT License](LICENSE) © 2026 三百斤的窜天猴
