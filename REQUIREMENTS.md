# 7z 密码表自动解压工具 — 开发需求

## 项目概述

在 Windows 上开发一个图形界面的解压缩软件，核心引擎使用开源的 **7-Zip (7z.exe)** 命令行程序。软件的重点功能是：**读取一个包含几十个密码的密码单，自动按顺序逐个尝试解压加密压缩包，直到找到正确密码并解压成功**。

## 技术栈

- 语言：Python 3（本项目机器上是 3.11）
- GUI：tkinter + ttk（标准库，无第三方依赖）
- 解压核心：调用 7-Zip 命令行 `7z.exe`（通过 subprocess 调用，不要使用 py7zr 等库）
- 项目名：`7z-pass-unlocker`，目录 `F:\code\7z-pass-unlocker`

## 功能需求

### 1. 基本解压
- 支持选择压缩包文件（.7z / .zip / .rar / .tar / .gz 等 7-Zip 支持的所有格式）
- 自动检测 7z.exe 位置，按以下顺序查找：
  1. 用户手动指定（界面上可填写/浏览）
  2. `C:\Program Files\7-Zip\7z.exe`
  3. `C:\Program Files (x86)\7-Zip\7z.exe`
  4. PATH 中的 `7z` 命令（用 `shutil.which`）
- 解压目标目录可配置，默认是压缩包所在目录下的 `<压缩包名>_解压` 文件夹

### 2. 密码单（核心功能）
- 密码来源两种：
  - **从文本文件导入**：每行一个密码，支持 UTF-8 / GBK 编码自动识别（用 `chardet` 不行，标准库没有，就用尝试 UTF-8 失败后回退 GBK 的方式）
  - **界面文本框手动输入**：多行文本框，每行一个密码
- 密码列表自动处理：去掉首尾空白、忽略空行、去重（保持首次出现顺序）
- **按顺序自动尝试**：从第一个密码开始，逐个用 `7z.exe -p<密码> x ...` 尝试解压
- 密码错误判定：7z 解压失败且 stderr/输出中包含 `Wrong password` 或 `Can not open encrypted archive` 或退出码非 0 → 判定为"密码错误"，继续尝试下一个
- 解压成功（退出码 0）→ 停止尝试，报告找到的密码
- 全部失败 → 报告失败统计
- 支持中途取消（点击"取消"按钮，终止当前 7z 子进程并停止后续尝试）
- 界面显示实时进度：当前尝试到第几个 / 总数、当前密码是什么、已尝试列表（成功/失败标记）、每个密码的耗时

### 3. 结果与日志
- 尝试结束后显示结果对话框：成功密码 / 全部失败
- 生成日志文件 `unlock_log.txt`，记录：时间、压缩包路径、成功密码（或失败统计）、总耗时
- 可选：成功解压后列出解压出的文件数量

### 4. GUI 要求
- 全部中文界面，标题："7z 密码自动解压工具"
- 布局：
  - 顶部：压缩包文件选择（Entry + 浏览按钮）
  - 第二行：7z.exe 路径（Entry + 自动检测 + 浏览按钮）
  - 第三行：解压目标目录（Entry + 浏览按钮）
  - 中部左侧：密码单文本区域（可编辑，带"导入密码文件"按钮、"清空"按钮）
  - 中部右侧或下方：尝试进度日志区域（ScrolledText，只读，实时追加）
  - 底部：进度条（ttk.Progressbar）、"开始尝试"按钮、"取消"按钮
- 使用 `threading.Thread` 在后台执行尝试，避免阻塞 GUI；用 `queue` + `after()` 轮询更新 UI（不要用 tkinter 的线程不安全 API）
- 支持把整个压缩包文件拖拽到窗口（可选，用 tkinterdnd2 不可用，如实现复杂可跳过，用按钮选择即可）

### 5. 代码结构
- 建议拆分为：
  - `main.py` — 程序入口 + GUI
  - `unlocker.py` — 核心逻辑（7z 调用、密码尝试、错误判定、日志），**纯逻辑无 GUI 依赖，方便命令行测试**
  - `README.md` — 使用说明
  - `requirements.txt` — 说明无需第三方依赖（Python 标准库即可）
- `unlocker.py` 要提供命令行可测的接口，例如：
  ```python
  class ZipUnlocker:
      def __init__(self, seven_zip_path: str, archive_path: str, dest_dir: str)
      def find_7z() -> str | None  # 静态方法，自动检测
      def try_passwords(self, passwords: list[str], progress_cb=None, cancel_event=None) -> dict
      # 返回 {"success": bool, "password": str|None, "attempted": int, "total": int, "errors": [...]}
  ```
- 子进程调用建议：
  ```
  7z.exe x -y -p<密码> -o<目标目录> <压缩包路径>
  ```
  注意：密码作为 `-p` 参数一部分传入；如果密码以 `-` 开头或含特殊字符，用 `-p` 后直接跟密码即可（7z 支持），但要注意 Windows 下引号转义。可以用 `subprocess.Popen` + `creationflags=subprocess.CREATE_NO_WINDOW`（Windows 上避免弹黑窗），stdout/stderr 捕获并解码（7z 输出可能是 GBK 编码，解码失败时用 errors='replace'）。
- 密码为空的情况：如果压缩包没加密，7z 直接解压成功；空密码行已被过滤，所以默认总是尝试传入密码。若列表为空提示用户。

## 验收标准

1. `python -m py_compile main.py unlocker.py` 无语法错误
2. 用 7z.exe 创建一个带密码的测试压缩包，写一个小的命令行冒烟测试脚本调用 `unlocker.py` 的 `try_passwords`，能按顺序尝试并找到正确密码（正确密码放在列表第 2、3 个位置验证"按顺序"逻辑）
3. GUI 能正常启动（在本机验证启动无异常即可，不要求截图）
