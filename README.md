# 河南工业大学校园网自动认证

**HAUT Campus Network Auto Login**

适用于河南工业大学深澜 SRun 校园网的自动认证工具。使用 Python 标准库，无需安装第三方依赖，支持交互输入学号与密码、Windows 加密保存密码，以及掉线自动重连。

默认认证网关为 `http://172.16.154.130`，认证区域为 `ac_id=1`。需要 Python 3.9 或以上版本；Windows 可直接使用附带的批处理文件。其他系统可通过终端运行 Python 脚本。

## 快速开始

1. 下载本项目，或克隆仓库：

   ```powershell
   git clone https://github.com/Willarix/haut-campus-auto-login.git
   cd haut-campus-auto-login
   ```

2. 连接河南工业大学校园网，运行：

   ```powershell
   python auto_login.py
   ```

3. 按提示输入自己的**学号**和**校园网密码**。密码输入时不会显示；本次输入不会自动保存。

Windows 用户也可以双击 `login.bat`。项目不包含任何预置的个人学号或密码，也不需要先创建配置文件。

## 保存账号与密码

Windows 下双击 `setup.bat`，或执行：

```powershell
python auto_login.py --setup
```

按提示输入学号、密码并确认密码。配置保存在脚本所在目录的 `config.json`，其中学号以明文保存，密码使用当前 Windows 用户的 DPAPI 加密保存。更换电脑或 Windows 用户后，需要重新配置。之后运行 `login.bat` 或 `watch.bat` 即可。

## 掉线自动重连

双击 `watch.bat`，或运行：

```powershell
python auto_login.py --watch
```

未配置账号时会先提示输入学号与密码。程序默认每 60 秒检查一次，离线后重新认证；同一账号已经在线时跳过登录。检测到其他账号在线时会提示并停止。

网络故障会逐渐延长重试间隔，默认上限 900 秒。认证被服务器拒绝时停止运行，请检查账号、密码和套餐后重新启动。按 `Ctrl+C` 停止。

自动重连期间需要保持程序运行。电脑重启后需要再次启动，或自行配置下文的任务计划程序。

## 常用命令

```powershell
# 只查询在线状态，无需输入学号或密码
python auto_login.py --status

# 跳过状态检查，直接尝试一次认证
python auto_login.py --force-login

# 指定其他配置文件
python auto_login.py --config "path/to/config.json" --watch

# 查看命令帮助
python auto_login.py --help
```

`--setup`、`--watch`、`--status`、`--force-login` 不能同时使用。退出码：`0` 表示成功或在线，`1` 表示状态查询结果为离线，`2` 表示配置错误或认证被拒绝，`3` 表示网络或协议错误，`130` 表示手动中止。

## 可选配置

默认配置可直接使用。需要修改网关、重试间隔等参数时，可复制 `config.example.json` 为 `config.json`，再按需编辑：

| 配置项 | 默认值或说明 |
| --- | --- |
| `server` | `http://172.16.154.130` |
| `username` | 空，运行时提示输入学号 |
| `password` | 空，运行时隐藏输入；建议使用 `--setup` 加密保存 |
| `ac_id` | `"1"` |
| `ip` | 空，从本次认证响应自动获取 |
| `timeout` | 单次请求超时，10 秒 |
| `interval` | 在线状态检查间隔，60 秒，最少 10 秒 |
| `retry_max_interval` | 网络故障重试间隔上限，900 秒 |

一般不需要填写 `ip`。每次认证都会重新获取 challenge 和客户端 IP，并重新计算认证参数；不要使用历史抓包中的认证摘要代替真实密码。

学号优先读取 `SRUN_USERNAME` 环境变量，再读取配置文件，最后提示输入。密码按以下顺序读取：

1. `SRUN_PASSWORD` 环境变量。
2. `--setup` 生成的 `password_dpapi` 加密字段。
3. 配置中的 `password` 明文字段。
4. 终端隐藏输入。

后台运行时如果缺少必要凭据，会直接报错退出。Linux 和 macOS 可通过环境变量或交互输入使用；`--setup` 加密保存功能仅支持 Windows。

## Windows 登录后自动启动（可选）

先运行 `--setup`，再确认 `--watch` 能正常工作。在 Windows「任务计划程序」中创建任务：

- 触发器：当前用户登录时，可延迟 30 秒等待校园网连接。
- 程序或脚本：本机 `python.exe` 的完整路径，可用 `python -c "import sys; print(sys.executable)"` 查询。
- 添加参数：`"项目完整路径\auto_login.py" --watch`。
- 起始于：项目文件夹的完整路径。
- 使用执行 `--setup` 时的同一 Windows 用户运行。
- 设置「如果任务已在运行」为「不启动新实例」，取消运行时长限制。

计划任务请直接启动 Python；批处理文件会等待按键，适合手动运行。

## 常见问题

- **无法连接认证服务器**：确认已连接校园网，并检查认证页面的网关地址是否与 `server` 一致。
- **认证被拒绝**：检查学号、真实登录密码和校园网套餐，按显示的错误码排查后重新启动。
- **Python 未找到**：安装 Python 3.9 或以上版本，并将 Python 加入 PATH。
- **密码解密失败**：在当前 Windows 用户下重新运行 `setup.bat` 或 `--setup`。
- **认证参数变化**：校园网升级后可能需要调整网关或协议参数。

本工具为非官方校园网客户端。当前网关使用 HTTP，认证参数中的编码不等同于传输加密，请勿公开完整认证请求、密码或个人配置。

## 工作原理

查询在线状态 → 获取新的 challenge 与当前 IP → HMAC-MD5 计算密码摘要 → xencode 与 SRun 定制 Base64 编码 `info` → SHA1 计算 `chksum` → 提交认证。

协议实现参考：[SRun 原始实现](https://github.com/vidar-team/srun-login/blob/master/pkg/srun/utils.go)。
