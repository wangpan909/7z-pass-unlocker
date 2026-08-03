# -*- coding: utf-8 -*-
"""
contextmenu.py — 右键菜单安装/卸载（Windows 注册表，当前用户级，免管理员权限）

在 Windows 资源管理器中注册右键菜单项：
    "使用密码单解压…"  → 以该文件为参数启动本程序。

支持两类目标：
  1. 常见压缩包扩展名（.7z/.zip/.rar 等）
  2. 通配键 "*"（所有文件）—— 资源分享者可能改后缀名，任意文件都可尝试解压
"""
import os
import subprocess
import sys
import time
import winreg

# 常见压缩包扩展名（含 7-Zip 支持的主要格式）
EXTENSIONS = [
    ".7z", ".zip", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".tbz2", ".txz", ".lzma", ".lz", ".zst", ".tzst", ".cab", ".iso",
    ".wim", ".swm", ".esd", ".001",
]

# 通配键：对任意后缀名的文件都显示右键菜单
ALLFILES = "*"

SHELLKEY = "Shell"
MENU_NAME = "UsePasswordList"          # 注册表键名（英文，避免乱码）
MENU_LABEL = "使用密码单解压"          # 显示的中文菜单名
MENU_ICON = "Icon"                     # 图标值名
CMDK = "command"                       # 命令子键名


def _targets(exts=None):
    """默认目标 = 常见扩展名 + 所有文件通配键。"""
    if exts is not None:
        return list(exts)
    return EXTENSIONS + [ALLFILES]


def _exe_path() -> str:
    """返回当前程序的 exe 完整路径（兼容 PyInstaller 单文件与源码运行）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable


def _command_line():
    """构造右键菜单 command： "<exe>" --auto "%1"  （%1 = 右键的文件路径，一键静默解压）"""
    return f'"{_exe_path()}" --auto "%1"'


def install_menu(exts=None) -> dict:
    """
    注册右键菜单项。
    :return: {"ok": bool, "installed": [目标...], "msg": str}
    """
    targets = _targets(exts)
    installed = []
    errors = []
    exe = _exe_path()
    cmd = _command_line()
    icon_val = f'"{exe}",0'

    for target in targets:
        # 键路径：HKCU\Software\Classes\<target>\Shell\UsePasswordList
        menu_key = rf"Software\Classes\{target}\{SHELLKEY}\{MENU_NAME}"
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, menu_key, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, MENU_LABEL)
                winreg.SetValueEx(k, MENU_ICON, 0, winreg.REG_SZ, icon_val)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                    rf"{menu_key}\{CMDK}", 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
            installed.append(target)
        except OSError as e:
            errors.append(f"{target}: {e}")

    return {"ok": not errors, "installed": installed, "msg": "、".join(errors)}


def uninstall_menu(exts=None) -> dict:
    """
    删除已注册的右键菜单项。
    :return: {"ok": bool, "removed": [目标...], "msg": str}
    """
    targets = _targets(exts)
    removed = []
    errors = []
    for target in targets:
        menu_key = rf"Software\Classes\{target}\{SHELLKEY}\{MENU_NAME}"
        try:
            # 必须先删子键（command），否则 DeleteKey 会失败
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                 rf"{menu_key}\{CMDK}")
            except FileNotFoundError:
                pass
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, menu_key)
            removed.append(target)
        except FileNotFoundError:
            pass  # 本来就没装，视为成功
        except OSError as e:
            errors.append(f"{target}: {e}")
    return {"ok": not errors, "removed": removed, "msg": "、".join(errors)}


def is_installed(exts=None) -> list:
    """返回已安装右键菜单的目标（扩展名/通配键）列表。"""
    targets = _targets(exts)
    found = []
    for target in targets:
        menu_key = rf"Software\Classes\{target}\{SHELLKEY}\{MENU_NAME}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, menu_key) as _:
                found.append(target)
        except FileNotFoundError:
            pass
    return found


def restart_explorer() -> bool:
    """重启资源管理器，使右键菜单立即生效（桌面/任务栏会闪一下）。
    返回是否成功执行。"""
    try:
        subprocess.run(["taskkill", "/f", "/im", "explorer.exe"],
                       capture_output=True)
        time.sleep(0.8)
        subprocess.Popen(["explorer.exe"])
        return True
    except Exception:
        return False


def _broadcast_refresh():
    """通知资源管理器刷新（SHCNE_ASSOCCHANGED）。"""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


if __name__ == "__main__":
    # 命令行自测：python contextmenu.py install|uninstall|status
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "install":
        res = install_menu()
        _broadcast_refresh()
        print("已安装:", res["installed"], "| 错误:", res["msg"] or "无")
    elif action == "uninstall":
        res = uninstall_menu()
        _broadcast_refresh()
        print("已卸载:", res["removed"], "| 错误:", res["msg"] or "无")
    else:
        print("当前已安装的目标:", is_installed() or "（无）")
