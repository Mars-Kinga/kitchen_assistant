# 厨房助手迁移包：在新电脑上运行

本包包含厨房 skill、Python Runtime、局域网控制台页面、440 道固定菜谱和安装启动脚本。交付版本另带本机已生成、已确认导入的菜谱；具体文件列在 `PACKAGE_MANIFEST.json`。这是独立运行程序，不需要安装 Codex、Node.js、Git、Docker 或机器人 SDK，目前使用模拟机器人。

不包含原电脑的密钥、虚拟环境、日志、照片和视频。首次安装需要联网下载 Python 依赖；本 ZIP 不是免安装 EXE，也不是完全离线安装包。安装完成后，本地菜谱和做菜流程可离线运行。AI 生成、图片识别、视频/图文理解需要联网和你自己的可用千问配置。会话与计时不跨进程恢复。

## 1. 新电脑要安装什么

- 普通 Windows 10/11 的 Intel/AMD 电脑：安装 **Python 3.13，Windows installer (64-bit)**。项目启动脚本接受 3.11–3.14，推荐 3.13。下载：[Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)。选择 3.13 系列的安装器，不要下载 embeddable package。
- 安装界面勾选 **Add python.exe to PATH**，保留 Python Launcher（`py`）。通常选择当前用户安装即可，无需管理员权限。[官方安装说明](https://docs.python.org/3.13/using/windows.html)
- 浏览器用现有 Edge、Chrome 等即可。
- Mac/Linux 同样需要 64 位 Python 3.11–3.14；Linux 安装器还需要系统已提供 `venv`。
- Windows ARM 电脑没有经过实机验证；优先使用系统支持的 x64 Python 兼容模式，避免原生 ARM 依赖缺少可用安装包。

其余基础依赖由 `install.cmd` 自动安装：OpenAI 兼容客户端、JSON Schema 校验、OpenCV 相机处理、带媒体工具的 imageio-ffmpeg 及其间接依赖。基础模式不需要另装 FFmpeg 或语音依赖。默认关闭语音播放。

## 2. Windows：第一次运行

1. 把 ZIP 复制到新电脑，右键选择“全部解压”。**不能直接在压缩包里双击运行。** 建议解压到可写的简单路径，例如 `C:\Users\你的用户名\KitchenAssistant`，避免 `Program Files`。
2. 打开解压后的 `kitchen-assistant-portable` 文件夹，双击 **`install.cmd`**。等待自动创建 `.venv`、下载依赖，看到“安装完成”后关闭窗口。下载失败时保留错误提示，联网后重新双击即可。
3. 如需 AI，按下一节填写配置。仅浏览固定菜谱和做菜指导可跳过。
4. 双击 **`start.cmd`**。控制台窗口保持打开，浏览器会自动打开本机页面；没有自动打开时，复制窗口打印的 `http://127.0.0.1:实际端口` 到浏览器。
5. 同一个 Wi-Fi/局域网的手机或其他电脑，打开启动窗口打印的 `http://电脑IP:实际端口`。不要在其他设备上输入 `127.0.0.1` 或 `0.0.0.0`。
6. 停止时在启动窗口按 **Ctrl+C**。以后只需双击 `start.cmd`，不用重复安装。

如果移动了已经安装好的文件夹，或换了电脑，请删除包内 `.venv` 后重新运行 `install.cmd`。虚拟环境不能跨电脑、跨系统直接复制，这是 [Python 官方 venv 文档](https://docs.python.org/3.13/library/venv.html) 的限制。

双击 **`check.cmd`** 可离线检查 Python、基础依赖、FFmpeg、skill 加载和端口配置。它不会调用 AI，不验证云端权限、真实相机或防火墙连通性。

如果需要同时输入文字：在本文件夹的终端中运行 `start.cmd --text`（PowerShell 使用 `.\start.cmd --text`）。默认双击启动的是纯网页控制台。

## 3. 配置 AI（可选）

安装脚本会创建 **`config/qwen.env`**，用记事本打开填写：

```ini
DASHSCOPE_API_KEY=你自己的有效密钥
QWEN_BASE_URL=你账号可访问的OpenAI兼容接口地址
QWEN_TEXT_MODEL=qwen3-omni-flash
QWEN_VISION_MODEL=qwen3-vl-flash
QWEN_TIMEOUT_SECONDS=45
QWEN_VISION_TIMEOUT_SECONDS=20
QWEN_MAX_RETRIES=0
```

现有示例中的接口地址是项目原来的服务实例地址，**请确认新电脑所用账号有访问权限，或改成自己的接口地址**。模型名称也应以账号实际授权为准。不要把 `QWEN_BASE_URL` 示例占位文字原样填进去。

程序自动读取 `config/qwen.env`，其中非空配置优先于同名环境变量。文件里的每项一行，勿增加 Markdown 反引号；保留 `.env` 扩展名，不要存成 `qwen.env.txt`。修改后停止并重新启动程序。

本包不携带原电脑的密钥。不要把填写过的 `qwen.env` 发给他人。图文导入复用上述视觉配置；视频理解默认使用 `qwen3-omni-flash`。额外的视频模型/超时或 Wellbyte 接口选项仍通过原程序支持的环境变量设置，通常无需配置。

如需主动验证千问连接，在本文件夹的 PowerShell 中运行（会发起实际云端请求）：

```powershell
.\.venv\Scripts\python.exe scripts\check_qwen_connection.py
```

## 4. 端口已改为 18765，冲突自动切换

本包和原程序的默认端口均为 **18765**。同一台电脑占用该端口时，依次尝试 **18766–18784**，使用第一个绑定成功的端口。不会结束占用端口的其他程序。启动窗口打印的地址始终包含实际使用的端口。

用记事本修改安装后生成的 **`config/console.json`**：

```json
{
  "host": "0.0.0.0",
  "port": 19876,
  "port_attempts": 20
}
```

- `port`：首选端口，可改为 1024–65535 中的其他端口；`0` 表示让系统分配空闲端口，重启后可能变化。
- `port_attempts`：最多连续尝试多少个端口，支持 1–100，且不超过 65535。设为 `1` 时只允许指定端口，失败就报告错误。
- `host`：`0.0.0.0` 允许局域网访问；改成 `127.0.0.1` 就只允许本机访问。

JSON 中不要加注释或多余逗号。保存后重启。Windows 预留端口也会尝试跳过；如果尝试范围均不可用，请另选一个首选端口。

**不同电脑使用同样的端口不会冲突**，访问地址里的 IP 不同。冲突是同一台电脑上的程序竞争同一监听地址和端口。

直接运行原入口也可修改端口：

```powershell
.\.venv\Scripts\python.exe robot_main.py --console-only --no-play --console-port 19876
```

`config/console.json` 由迁移启动脚本读取；直接运行 `robot_main.py` 时，以命令行参数为准。

## 5. Windows 局域网与摄像头

- 首次启动若出现 Windows 防火墙提示，允许 Python 在所用的可信专用网络通信即可。如果本机能打开、手机打不开，检查是否同一网络、是否允许包内 `.venv\Scripts\python.exe` 入站，以及路由器是否启用了访客网络隔离。
- 如果防火墙规则按端口放行，需要放行**实际使用端口**；固定只允许 18765 的规则在自动切换后不会生效。规则由单位管理时，请让管理员放行实际端口或 Python 程序。
- 启动窗口可能列出多个网卡地址；选与手机所在网络相同的那个。也可在 Windows 的网络设置或 `ipconfig` 中查看 Wi-Fi/以太网 IPv4 地址，然后拼成 `http://地址:实际端口`。
- 本机相机使用 OpenCV 系统默认后端，Mac 使用 AVFoundation。Windows 需在“设置 → 隐私和安全性 → 相机”允许桌面应用访问相机，关闭占用相机的会议软件。没有相机也可上传照片或在手机网页选择拍摄照片。
- 手机“拍照”使用浏览器/系统提供的文件选择能力；实际是否显示拍摄入口取决于手机浏览器。

## 6. Mac / Linux 操作

解压后，在终端进入包目录：

```bash
bash install.sh
# 按需修改 config/qwen.env
bash start.sh
# 可选：同时文字交互
bash start.sh --text
# 可选：离线诊断
bash check.sh
```

Linux 若提示缺少 `venv`、`libGL.so.1` 或 `libglib`，需按所用发行版安装对应系统组件后重试；桌面摄像头也需要设备访问权限。

## 7. 数据、后续重新打包与验证范围

- 已确认导入菜谱保存在 `skills/kitchen_assistant/recipes/imported/`；AI 缓存在 `skills/kitchen_assistant/recipes/generated/`。迁移启动不会删除这些文件。
- 当前厨房会话和未确认草稿只在内存中。关闭程序会结束进程中的计时和会话。
- 在项目/包目录重新打包：`python scripts/build_portable.py --include-local-recipes`。输出 `dist/kitchen-assistant-portable.zip` 及 SHA-256 校验文件。去掉此选项会只打包固定目录和程序，不带个人运行菜谱。
- 重新打包采用文件白名单，不包含 `config/qwen.env`、`config/console.json`、`.venv`、日志、媒体、测试缓存或 Git 数据。你手动改过的端口需在新电脑重新配置。
- 本次交付在 macOS 上验证程序与打包流程、端口冲突切换、包解压后的安装与 HTTP 控制台；Windows 双击脚本已编写，尚未进行 Windows 实机验证。目标电脑的相机权限、网络策略和 AI 账号需当地确认。

默认不启用语音录制或播报。需要语音时可另安装 `requirements-voice.txt` 并配置原项目的 ASR/TTS 环境；这不属于本包的基础启动流程。
