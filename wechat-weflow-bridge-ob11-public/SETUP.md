# 从零开始搭建指南

> 这是一份写给全新电脑的完整搭建教程，假设电脑上什么都没有。

---

## 📦 需要安装的东西

| 软件 | 用途 | 下载 |
|------|------|------|
| Python 3.10+ | 运行桥接脚本 | https://www.python.org/downloads |
| Git | 下载项目代码（可选） | https://git-scm.com/downloads |
| WeFlow | 微信消息中转 | https://weflow.top |
| AstrBot | AI 机器人框架 | https://github.com/AstrBotDevs/AstrBot |
| ollama（可选） | 本地图片识别 | https://ollama.ai |

---

## 第 1 步：安装 Python

1. 打开 https://www.python.org/downloads，点击大大的黄色 **Download Python** 按钮
2. 下载完成后运行安装包
3. **☑️ 务必勾选 "Add Python to PATH"**（在安装界面最下面）
4. 点击 "Install Now"
5. 验证安装成功：打开命令提示符（按 `Win + R` → 输入 `cmd` → 回车），输入：

```bash
python --version
```

应该显示 `Python 3.10` 或更高版本。

## 第 2 步：安装 Git（可选）

如果会用 Git，可以用它来下载项目：

1. 打开 https://git-scm.com/downloads
2. 下载安装，一路默认下一步
3. 验证安装：

```bash
git --version
```

> 如果不想装 Git，也可以直接下载 ZIP 压缩包。

## 第 3 步：下载项目

**方式一：Git 克隆**

```bash
git clone https://github.com/你的用户名/wechat-weflow-bridge-ob11.git
cd wechat-weflow-bridge-ob11
```

**方式二：下载 ZIP**

1. 打开 GitHub 项目页面
2. 点绿色的 "Code" → "Download ZIP"
3. 解压到桌面

## 第 4 步：安装项目依赖

在项目目录下打开命令提示符，运行：

```bash
pip install -r requirements.txt
```

等待安装完成（可能需要 1-2 分钟）。

## 第 5 步：安装并配置 WeFlow

WeFlow 是微信和桥接之间的消息中转站。

1. 打开 https://weflow.top 下载安装
2. 打开 WeFlow，用微信扫码登录
3. 进入 WeFlow 设置 → **开启 API 服务**（默认端口 5031）
4. 找到 **Access Token**，复制下来（后续要用）

> ⚠️ WeFlow 需要保持运行，不能关闭。

## 第 6 步：部署 AstrBot

AstrBot 是 AI 机器人框架，负责调用 DeepSeek、Kimi 等大模型。

**推荐方式：Docker 部署（最简单）**

```bash
docker run -d -p 11229:11229 -p 6185:6185 --name astrbot \
  -v /path/to/data:/AstrBot/data \
  ghcr.io/astrbotdevs/astrbot:latest
```

或者参考 AstrBot 官方文档用一键包部署。

### 配置 aiocqhttp 适配器

在 AstrBot 的 WebUI（http://127.0.0.1:6185）中：

1. 进入「配置」→「平台适配器」
2. 添加一个 **aiocqhttp** 适配器：

| 字段 | 值 |
|------|-----|
| ID | `wechat_bridge` |
| 类型 | `aiocqhttp` |
| 启用 | ☑️ |
| ws_reverse_host | `0.0.0.0` |
| ws_reverse_port | `11229` |
| ws_reverse_token | 留空 |

3. 保存并重启 AstrBot

### 配置 LLM（大模型）

在 AstrBot WebUI 中：
1. 「配置」→「LLM 提供商」→ 添加提供商
2. 填入你的 API Key（DeepSeek / Kimi / Claude 等）

## 第 7 步：首次启动桥接

在项目目录下运行：

```bash
python main.py
```

首次启动会自动创建 `config.json`。你会看到类似输出：

```
[INFO] 使用 UIA 发送消息（微信 4.0+）
[INFO] 正在搜索微信窗口...
[INFO] 微信窗口: '微信'
[INFO] Web: http://127.0.0.1:8766
[INFO] 已连接到 WeFlow 推送
[INFO] 已连接到 AstrBot
```

## 第 8 步：Web 面板配置

打开浏览器访问 **http://127.0.0.1:8766**

1. 点左侧 **「基础设置」**
2. 填写以下必填项：

| 字段 | 填什么 |
|------|--------|
| `access_token` | WeFlow 设置里复制的那串 Token |
| `astrbot_ob_url` | `ws://127.0.0.1:11229/ws`（如果 AstrBot 在别的机器则改 IP） |
| `bot_nicknames` | 你机器人的微信昵称，用于群聊 @ 检测 |
| `group_reply_mode` | `mention`（仅@回复）/ `all`（全部回复） |

3. 点 **「保存配置」**
4. 点左侧 **「控制面板」** → 点 **「停止」** → 再点 **「启动」** 让配置生效

## 第 9 步：验证是否成功

给机器人微信发一条消息，看流程是否走通：

```
你发 "你好" → 微信
```

观察终端或 Web 面板的日志，应该看到：

```
📩 收到: 你的昵称 → 你好
✅ 已推送至 AstrBot 客户端
🤖 (AstrBot 处理中)
[OB11] API: send_private_msg
[UIA✓] 你的昵称: (AI 回复内容)
```

## 可选：图片识别

如果想让 AI 能看懂你发的图片：

**方案 A：Ollama 本地（免费但需要 GPU）**

```bash
# 安装 ollama
https://ollama.ai

# 拉取视觉模型
ollama pull llava:7b
```

然后在 Web 面板「基础设置」中确保：
- 描述服务 = `ollama`
- 模型名 = `llava:7b`

**方案 B：Kimi 云端（无需 GPU，首次加载快）**

在 Web 面板「基础设置」中设置：
- 描述服务 = `openai`
- 模型名 = `kimi-k2.6`
- API Key = 你的 Moonshot API Key
- API 地址 = `https://api.moonshot.cn/v1`

---

## 目录结构

```
wechat-weflow-bridge-ob11/
├── main.py              # 入口（启动这个）
├── config.example.json  # 配置模板（首次启动自动复制为 config.json）
├── requirements.txt     # Python 依赖
├── start.bat            # Windows 快捷启动
└── ... 其他模块文件
```

## 常见问题

**Q: 启动报错 "No module named xxx"**
A: 没装依赖，运行 `pip install -r requirements.txt`

**Q: 连不上 WeFlow**
A: 确认 WeFlow 已打开并登录微信，API 服务已开启

**Q: 连不上 AstrBot**
A: 确认 AstrBot 已启动，aiocqhttp 适配器已启用且端口正确

**Q: 消息发出去了但 AI 没回复**
A: 检查 AstrBot 的 LLM 配置是否正确，API Key 是否有效
