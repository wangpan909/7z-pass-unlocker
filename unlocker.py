# -*- coding: utf-8 -*-
"""
unlocker.py — 7z 密码表自动解压核心逻辑（纯逻辑，无 GUI 依赖，可命令行测试）

本模块负责：
  1. 自动检测 7z.exe 位置
  2. 调用 7z.exe 执行解压
  3. 逐个尝试密码，直到成功或全部失败
  4. 错误判定（Wrong password / Can not open encrypted archive 等）
  5. 生成日志 unlock_log.txt

命令行调用示例：
    from unlocker import ZipUnlocker, load_password_file
    u = ZipUnlocker(r"C:\\Program Files\\7-Zip\\7z.exe", "a.7z", "a_解压")
    pws = load_password_file("pass.txt")
    result = u.try_passwords(pws)
    print(result)
"""

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

# 7z 输出中使用的中英文错误特征串，用于判断密码错误
WRONG_PASSWORD_PATTERNS = [
    "wrong password",
    "can not open encrypted archive",
    # 中文版 7-Zip 的错误提示（如果本机 7z 输出中文）
    "密码不正确",
    "无法打开加密的档案",
]

# 日志文件名
LOG_FILE_NAME = "unlock_log.txt"
LOG_FILE = LOG_FILE_NAME


def get_default_password_file() -> str:
    """
    返回默认密码单文件路径：%APPDATA%\7z密码解压助手\passwords.txt
    该文件用于“永久维护”一份密码列表，程序启动时自动加载。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "7z密码解压助手", "passwords.txt")


def save_default_password_list(passwords: list) -> str:
    """
    将密码列表保存到默认密码单文件（自动创建目录）。
    返回文件完整路径。
    """
    path = get_default_password_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for p in passwords:
            f.write(p.strip() + "\n")
    return path


class ZipUnlocker:
    """7z 密码自动尝试解压器。"""

    def __init__(self, seven_zip_path: str, archive_path: str, dest_dir: str):
        """
        :param seven_zip_path: 7z.exe 完整路径
        :param archive_path:   压缩包完整路径
        :param dest_dir:       解压目标目录（会自动创建）
        """
        self.seven_zip_path = seven_zip_path
        self.archive_path = archive_path
        self.dest_dir = dest_dir
        self._current_process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 静态方法：自动检测 7z
    # ------------------------------------------------------------------
    @staticmethod
    def find_7z() -> str | None:
        """
        按顺序自动检测 7z.exe：
          1. 常见安装路径
          2. PATH 中的 7z 命令
        返回找到的路径，未找到返回 None。
        """
        candidates = [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        # 试试环境变量 7Z 或用户配置目录
        env_7z = os.environ.get("7Z")
        if env_7z and os.path.isfile(env_7z):
            return env_7z
        return _find_7z_in_path_win()

    # ------------------------------------------------------------------
    # 子进程管理（供取消使用）
    # ------------------------------------------------------------------
    def terminate_current(self) -> None:
        """终止当前正在运行的 7z 子进程（用于"取消"按钮）。"""
        with self._process_lock:
            p = self._current_process
            if p is not None:
                try:
                    p.terminate()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 核心：尝试一组密码
    # ------------------------------------------------------------------
    def try_passwords(self, passwords: list[str],
                      progress_cb=None, cancel_event=None) -> dict:
        """
        逐个尝试密码。

        :param passwords:     密码列表（已预处理：去空白、去空行、去重）
        :param progress_cb:   可选回调，参数 (index, total, password, status, detail, elapsed)，
                              status 取值：running / success / failed
        :param cancel_event:  可选 threading.Event，置位时取消后续尝试
        :return: dict，形如：
              {
                "success": bool,
                "password": str | None,
                "attempted": int,
                "total": int,
                "errors": list[str],
              }
        """
        total = len(passwords)
        result = {
            "success": False,
            "password": None,
            "attempted": 0,
            "total": total,
            "errors": [],
        }
        if total == 0:
            result["errors"].append("密码列表为空")
            return result

        for idx, pwd in enumerate(passwords):
            # 检查取消信号
            if cancel_event is not None and cancel_event.is_set():
                result["errors"].append("用户取消")
                break

            result["attempted"] = idx + 1
            start = time.perf_counter()

            if progress_cb:
                progress_cb(idx + 1, total, pwd, "running", "正在尝试...", 0)

            # 执行解压
            returncode, out_text, err_text = self._run_7z(pwd)

            elapsed_ms = int((time.perf_counter() - start) * 1000)

            # 判定成功 / 失败
            if self._is_success(returncode, out_text, err_text):
                result["success"] = True
                result["password"] = pwd
                if progress_cb:
                    progress_cb(idx + 1, total, pwd, "success",
                                "解压成功！", elapsed_ms)
                break
            else:
                detail = self._failure_reason(out_text, err_text)
                if progress_cb:
                    progress_cb(idx + 1, total, pwd, "failed",
                                detail, elapsed_ms)
        else:
            # for-else：未 break 且未中途取消，说明全部失败
            if cancel_event is None or not cancel_event.is_set():
                result["success"] = False

        return result

    # ------------------------------------------------------------------
    # 单次 7z 调用
    # ------------------------------------------------------------------
    def _run_7z(self, password: str) -> tuple[int, str, str]:
        """执行 7z 解压单次尝试，返回 (返回码, stdout文本, stderr文本)。"""
        if not os.path.isdir(self.dest_dir):
            os.makedirs(self.dest_dir, exist_ok=True)

        # 构建命令行： 7z.exe x -y -p<密码> -o<目标目录> <压缩包>
        cmd = [self.seven_zip_path,
               "x", "-y",
               f"-p{password}",
               f"-o{self.dest_dir}",
               self.archive_path]

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW  # 避免弹黑窗

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except Exception as e:  # noqa: BLE001 - 需要捕获所有异常向上传递
            return -1, "", f"启动 7z 失败: {e}"

        with self._process_lock:
            self._current_process = proc
        try:
            out_bytes, err_bytes = proc.communicate()
        finally:
            with self._process_lock:
                self._current_process = None

        # 解码：7z 输出可能是 GBK，统一用 utf-8 尝试，失败回退 GBK
        out_text = _decode_bytes(out_bytes)
        err_text = _decode_bytes(err_bytes)
        return proc.returncode, out_text, err_text

    # ------------------------------------------------------------------
    # 结果判定
    # ------------------------------------------------------------------
    def _is_success(self, return_code: int, out_text: str, err_text: str) -> bool:
        """根据返回码与输出判定本次尝试是否成功。"""
        if return_code == 0:
            return True
        # 密码错误判定：返回码非 0（Wrong password / Can not open encrypted archive 等）
        return False

    def _failure_reason(self, out_text: str, err_text: str) -> str:
        """根据 7z 输出推断失败原因，用于界面/日志展示。"""
        combined = (out_text + "\n" + err_text).lower()
        if "wrong password" in combined or "密码不正确" in combined:
            return "密码错误"
        if "can not open encrypted archive" in combined or "无法打开加密的档案" in combined:
            return "无法打开加密档案"
        if not os.path.isfile(self.archive_path):
            return "压缩包不存在"
        return "解压失败"

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def write_log(self, result: dict) -> str:
        """生成 unlock_log.txt，返回日志文件完整路径。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if result.get("success"):
            lines = [
                "=" * 60,
                f"[{ts}] 压缩包: {self.archive_path}",
                f"目标目录: {self.dest_dir}",
                f"结果: 成功",
                f"正确密码: {result['password']}",
                f"尝试次数: {result['attempted']} / {result['total']}",
                "=" * 60,
            ]
        else:
            lines = [
                "=" * 60,
                f"[{ts}] 压缩包: {self.archive_path}",
                f"目标目录: {self.dest_dir}",
                f"结果: 失败（未找到正确密码）",
                f"尝试次数: {result['attempted']} / {result['total']}",
            ]
            if result.get("errors"):
                lines.append("错误信息:")
                for e in result["errors"]:
                    lines.append(f"  - {e}")
            lines.append("=" * 60)

        # 日志写在压缩包所在目录，方便查看
        log_dir = os.path.dirname(os.path.abspath(self.archive_path))
        if not os.path.isdir(log_dir):
            log_dir = os.getcwd()
        log_path = os.path.join(log_dir, LOG_FILE)
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:  # noqa: BLE001 - 日志失败不影响主流程
            return ""
        return log_path


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------
def _find_7z_in_path_win() -> str | None:
    """在 PATH 中查找 7z（shutil.which），兼容非 Windows 环境。"""
    path = shutil.which("7z")
    if path:
        return path
    path = shutil.which("7za")
    if path:
        return path
    return None


def _decode_bytes(data: bytes) -> str:
    """将 7z 输出字节解码为文本，优先 UTF-8，失败回退 GBK，再失败用 errors='replace'。"""
    if data is None:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def load_password_list(file_path: str) -> list[str]:
    """
    从文本文件读取密码列表，自动识别 UTF-8 / GBK 编码。
    每行一个密码：去首尾空白、忽略空行、去重（保持首次出现顺序）。
    """
    raw = None
    with open(file_path, "rb") as f:
        raw = f.read()

    text = _decode_bytes(raw)

    # 按行拆分，去首尾空格，忽略空行（同时清理 BOM）
    items = []
    seen = set()
    for line in text.splitlines():
        p = line.strip().lstrip("\ufeff")
        if not p:
            continue
        if p not in seen:
            seen.add(p)
            items.append(p)
    return items