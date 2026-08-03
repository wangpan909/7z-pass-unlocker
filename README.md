# 7z密码单解压助手

基于 **7-Zip** 命令行工具（`7z.exe`）的 Windows 图形界面解压软件。
核心功能：读取包含几十个密码的密码单，**按顺序逐个自动尝试解压加密压缩包**，
直到找到正确密码并成功解压。

- 语言：Python 3（标准库，无第三方依赖）
- GUI：tkinter + ttk
- 解压核心：调用 `7z.exe`（subprocess）

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `main.py` | 程序入口 + tkinter 图形界面 |
| `unlocker.py` | 核心逻辑（7z 调用、密码尝试、错误判定、日志），无 GUI 依赖，可命令行测试 |
| `requirements.txt` | 依赖说明（无需第三方库） |
| `README.md` | 使用说明 |

## 运行

```bash
python main.py
```

GUI 会自动检测 `7z.exe`，检测顺序：

1. 界面手动指定
2. `C:\Program Files\7-Zip\7z.exe`
3. `C:\Program Files (x86)\7-Zip\7z.exe`
4. PATH 中的 `7z` / `7za` 命令

## 使用方法

**方式 A：右键一键解压（推荐）** —— 无需打开界面
- 右键任意压缩包（或任意后缀文件）→「**使用密码单解压**」→ 自动完成。
- 解压目录：压缩包同名目录（如 `xxx.zip` → 解压到 `xxx\`）。
- 成功后自动删除源压缩包；失败弹窗提示，源文件保留。

**方式 B：图形界面手动解压**
1. 打开程序，选择压缩包（支持 7-Zip 的所有格式）。
2. 确认 `7z.exe` 路径（可点「自动检测」）。
3. 解压目标目录默认 = 原文件名目录（可改）。
4. 密码单自动加载默认列表；可编辑或点「导入密码文件」。
5. 点「开始尝试」，界面实时显示进度，可随时「取消」。

## 安装版（绿色 exe + 右键菜单）

已用 PyInstaller 打包为免安装的绿色单文件：`dist\7z密码单解压助手.exe`（约 10MB，目标机器**无需安装 Python**，但需已安装 7-Zip）。

### 使用方法（两步）

1. **安装右键菜单**（只需一次）：
   - 双击 exe 打开界面 → 底部「右键菜单」区点「**安装右键菜单**」；
   - 或命令行执行：`7z密码单解压助手.exe --install-menu`
   - **安装后程序会询问是否重启资源管理器**，选择“是”即可立刻生效；
     若选否，需手动重启资源管理器或注销（否则右键菜单可能不显示）。
2. **日常使用**：在资源管理器中右键压缩包 → 点「**使用密码单解压**」→
   **自动静默解压**（隐藏窗口，后台按顺序尝试默认密码单）：
   - **成功**：无任何弹窗，解压到原文件名目录，并自动删除源压缩包；
   - **失败**：弹窗提示“未能解压 / 密码不对”，源文件保留。

> **支持任意后缀名**：除常见格式（.7z/.zip/.rar 等 20 种）外，
> 还注册了“**所有文件**”通配项 —— 即使资源分享者把文件改成任意后缀
> 名（如 `.abc123`、`.dat`、`.001`），右键也都会出现该菜单，
> 程序会按文件内容识别格式并自动尝试解压。

### 命令行动作

| 命令 | 作用 |
| --- | --- |
| `7z密码单解压助手.exe` | 打开图形界面 |
| `7z密码单解压助手.exe "a.7z"` | 打开界面并预填压缩包（右键菜单即此方式） |
| `7z密码单解压助手.exe --install-menu` | 静默安装右键菜单 |
| `7z密码单解压助手.exe --uninstall-menu` | 静默卸载右键菜单 |
| `7z密码单解压助手.exe --status` | 查询右键菜单安装状态 |

> 说明：右键菜单写入当前用户注册表（HKCU），无需管理员权限；
> 卸载时删除注册表项，不留残余。exe 可放在任意目录，移动到新位置后重新点一次「安装右键菜单」即可更新路径。


## 永久维护的默认密码单

程序会**记住一份默认密码单**，保存在：

```
%APPDATA%\7z密码单解压助手\passwords.txt
```

- **启动自动加载**：每次打开程序，密码单区自动填入这份列表，无需重复输入。
- **日常维护**：在密码单区增删改后，点「**保存密码单**」按钮即可永久更新，
  下次启动自动生效。
- 初始已预置你常用的 11 个密码（hmoe.top、xxld、wnovo、Drgon Slayer、
  人人素材、yecgaa、blackcatunderthemoon、小猫喝奶啤、猫里奥小新、xcys、FLYYZ）。

> 提示：该文件可直接用文本编辑器打开编辑（每行一个密码），
> 保存后无需重启程序，下次启动即生效。

## 命令行 / 冒烟测试

核心逻辑完全独立于 GUI，便于命令行测试：

```python
from unlocker import ZipUnlocker, load_password_list

unlocker = ZipUnlocker(
    seven_zip_path=r"C:\Program Files\7-Zip\7z.exe",
    archive_path="test_encrypted.7z",
    dest_dir="test_out",
)

passwords = load_password_list("passwords.txt")   # 或直接传列表
result = unlocker.try_passwords(passwords)
print(result)
```

`try_passwords` 返回结构：

```python
{
    "success": bool,       # 是否解压成功
    "password": str|None,  # 成功时的正确密码；失败为 None
    "attempted": int,      # 实际尝试次数
    "total": int,          # 密码总数
    "errors": [str],       # 错误信息列表（如"密码列表为空"、"用户取消"）
}
```

## 依赖

`requirements.txt` 说明：**无需任何第三方依赖**，仅需 Python 3 标准库。

## 验收

```bash
python -m py_compile main.py unlocker.py   # 语法检查
```