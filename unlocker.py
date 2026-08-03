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
                      progress_cb=None, cancel_event=None,
                      overwrite: str = "overwrite") -> dict:
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
            returncode, out_text, err_text = self._run_7z(pwd, overwrite=overwrite)

            elapsed_ms = int((time.perf_counter() - start) * 1000)

            # 判定成功 / 失败
            if self._is_success(returncode, out_text, err_text):
                result["success"] = True
                result["password"] = pwd
                # 修复因文件名编码导致的乱码（SJIS/GBK 无 UTF-8 标志的 ZIP）
                self._fix_filename_encoding()
                # 智能收缩：包内仅一个顶层目录时不再额外套一层（类似 Bandizip 智能解压）
                self._collapse_single_dir()
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
    def _run_7z(self, password: str,
                overwrite: str = "overwrite") -> tuple[int, str, str]:
        """执行 7z 解压单次尝试，返回 (返回码, stdout文本, stderr文本)。
        overwrite: overwrite|skip|rename —— 控制重名文件处理。"""
        if not os.path.isdir(self.dest_dir):
            os.makedirs(self.dest_dir, exist_ok=True)

        # 构建命令行： 7z.exe x <overwrite> -p<密码> -o<目标目录> <压缩包>
        # overwrite: "overwrite"->-y(覆盖) "skip"->-aos(跳过已存在) "rename"->-aou(自动改名)
        ov = {"overwrite": "-y", "skip": "-aos", "rename": "-aou"}.get(overwrite, "-y")
        cmd = [self.seven_zip_path,
               "x", ov,
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
    # 文件名编码修复（动画/日文资源包常见问题）
    # ------------------------------------------------------------------
    def _fix_filename_encoding(self) -> None:
        """
        修复因文件名编码导致的乱码（仅对 ZIP 系容器）。

        问题来源：部分 ZIP（尤其日文/动画资源包）文件名记录的是 Shift-JIS
        或 UTF-8 字节，但通用标志位未置 UTF-8 位（0x0800），7-Zip 会按系统
        本地代码页（中文系统为 GBK）误读，导致解出的文件/文件夹名乱码。

        解决：读取 ZIP 中央目录中的原始文件名字节，自动探测编码
        （UTF-8 → Shift-JIS → GBK），建立“乱码名 → 正确名”映射，
        然后递归逐层重命名（先目录后文件，目录修复后继续进入处理）。
        """
        try:
            ext = os.path.splitext(self.archive_path)[1].lower()
            if ext not in (".zip", ".jar", ".apk", ".docx", ".xlsx",
                           ".pptx", ".epub"):
                return
            if not os.path.isdir(self.dest_dir):
                return
            entries = _zip_central_entries(self.archive_path)
            if not entries:
                return
            # 建立单层名称映射：GBK误读名 -> 正确名（目录/文件各层组件）
            name_map = {}
            for raw_path, raw in entries:
                mangled_path = raw_path.decode("gbk", errors="replace")
                proper_path = raw_path.decode(_detect_name_encoding(raw_path))
                if mangled_path == proper_path:
                    continue
                m_parts = mangled_path.split("/")
                p_parts = proper_path.split("/")
                for mp, pp in zip(m_parts, p_parts):
                    if mp and mp != pp:
                        name_map[mp] = pp
            if not name_map:
                return
            self._fix_name_level(self.dest_dir, name_map)
        except Exception:
            pass  # 修复失败不影响主流程

    def _fix_name_level(self, d: str, name_map: dict) -> None:
        """递归修复一层目录：先重命名本层乱码项，再进入子目录递归。"""
        try:
            names = os.listdir(d)
        except OSError:
            return
        for entry in names:
            full = os.path.join(d, entry)
            new_name = name_map.get(entry)
            if new_name and new_name != entry:
                dst = os.path.join(d, new_name)
                if not os.path.exists(dst):
                    try:
                        os.rename(full, dst)
                        entry = new_name
                        full = os.path.join(d, entry)
                    except OSError:
                        pass
            # 进入子目录继续修复（目录可能刚被重命名）
            if os.path.isdir(full):
                self._fix_name_level(full, name_map)


    def _collapse_single_dir(self) -> None:
        """
        智能解压收缩：若压缩包内所有文件都位于【同一个顶层目录】（且无散文件），
        则解压后将该顶层目录层折叠掉，内容直接出现在解压目标目录。

        例如 video.zip 内含 video/xxx.mp4 ——
        解压后目标目录下直接是 xxx.mp4，不再多出 video/ 这一层。
        若包内存在多个顶层目录或散文件，则保持原样不动。
        """
        try:
            if not os.path.isdir(self.dest_dir):
                return
            entries = _zip_central_entries(self.archive_path)
            if not entries:
                return
            # 提取所有条目的顶层路径段（去重）
            tops = set()
            for raw_path, _ in entries:
                proper = raw_path.decode(_detect_name_encoding(raw_path))
                top = proper.split("/")[0]
                tops.add(top)
            if len(tops) != 1:
                return  # 多顶层/散文件，保留结构
            top = tops.pop()
            sub = os.path.join(self.dest_dir, top)
            if not os.path.isdir(sub):
                return
            # 上移该顶层目录的全部内容
            for name in os.listdir(sub):
                s = os.path.join(sub, name)
                d = os.path.join(self.dest_dir, name)
                if os.path.exists(d):
                    continue  # 目标重名，跳过避免覆盖
                try:
                    shutil.move(s, d)
                except OSError:
                    pass
            # 删除已清空的顶层目录
            try:
                if not os.listdir(sub):
                    os.rmdir(sub)
            except OSError:
                pass
        except Exception:
            pass  # 收缩失败不影响主流程

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------
    def check_conflicts(self) -> list:
        """
        检查解压目标目录中是否已有同名文件/文件夹（可能与压缩包解压结果冲突）。
        返回冲突的顶层条目名列表；无冲突返回空列表。
        注意：解压到目标目录后（含智能折叠），顶层冲突项会被覆盖/跳过。
        """
        try:
            if not os.path.isdir(self.dest_dir):
                return []
            tops = []
            ext = os.path.splitext(self.archive_path)[1].lower()
            zip_like = ext in (".zip", ".jar", ".apk", ".docx", ".xlsx",
                               ".pptx", ".epub")
            if zip_like:
                entries = _zip_central_entries(self.archive_path)
                for raw_path, _ in entries:
                    proper = raw_path.decode(
                        _detect_name_encoding(raw_path))
                    top = proper.replace("\\", "/").split("/")[0]
                    if top not in tops:
                        tops.append(top)
            else:
                # 非 zip：解析 7z l -slt
                cmd = [self.seven_zip_path, "l", "-slt", self.archive_path]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0)
                out_bytes, _ = proc.communicate()
                text = _decode_bytes(out_bytes)
                import re
                first = True
                for m in re.finditer(r"^Path = (.+)$", text, re.M):
                    p = m.group(1).strip().replace("\\", "/").lstrip("/")
                    if not p:
                        continue
                    if first:
                        first = False
                        continue
                    top = p.split("/")[0]
                    if top not in tops:
                        tops.append(top)
            return [t for t in tops if os.path.exists(os.path.join(self.dest_dir, t))]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 解压内容预览
    # ------------------------------------------------------------------
    def get_extract_preview(self) -> str:
        """
        读取压缩包内容，返回解压后【最终顶层条目】的文字描述，
        供进度窗口显示"解压出来会得到什么"。

        策略：
          - ZIP 系（.zip/.jar/.apk 等）：用 _zip_central_entries 读取原始
            文件名字节，经 _detect_name_encoding 精确解码（正确还原日文/
            中文），无需依赖 7z 的控制台编码。
          - 其他格式（rar/7z/tar 等）：解析 `7z l -slt` 的 Path=，用
            _decode_bytes 兜底解码。
        再应用“单顶层目录折叠”规则：若包内仅一个顶层目录，则返回该目录
        内部的文件名（最终会直接散在压缩包旁边）。
        """
        try:
            # ---- ZIP 系：用中央目录原始字节，最精确 ----
            ext = os.path.splitext(self.archive_path)[1].lower()
            zip_like = ext in (".zip", ".jar", ".apk", ".docx", ".xlsx",
                               ".pptx", ".epub")
            if zip_like:
                entries = _zip_central_entries(self.archive_path)
                if entries:
                    # 顶层段（已按正确编码解码）
                    tops = []
                    raw_paths = []
                    for raw_path, _ in entries:
                        proper = raw_path.decode(
                            _detect_name_encoding(raw_path))
                        p = proper.replace("\\", "/")
                        top = p.split("/")[0]
                        if top not in tops:
                            tops.append(top)
                        raw_paths.append(p)
                    return self._preview_fold(tops, raw_paths)
            # ---- 非 ZIP：解析 7z l -slt 的 Path ----
            cmd = [self.seven_zip_path, "l", "-slt", self.archive_path]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0)
            out_bytes, _ = proc.communicate()
            text = _decode_bytes(out_bytes)
            tops = []
            raw_paths = []
            import re
            first = True
            for m in re.finditer(r"^Path = (.+)$", text, re.M):
                p = m.group(1).strip().replace("\\", "/").lstrip("/")
                if not p:
                    continue
                if first:
                    first = False
                    continue  # 第一个 Path 是压缩包自身路径，跳过
                top = p.split("/")[0]
                if top not in tops:
                    tops.append(top)
                raw_paths.append(p)
            return self._preview_told if False else self._preview_fold(tops, raw_paths)
        except Exception:
            return ""

    def _preview_fold(self, tops: list, raw_paths: list) -> str:
        """生成简洁解压内容预览：
        - 单顶层目录：显示该目录名
        - 单顶层文件：显示 /文件名
        - 多个：显示前 3 个 + "等N项" （避免一大串）"""
        if not tops:
            return ""
        # 判断每个顶层是文件还是目录（若存在以 top/ 开头的条目则为目录）
        is_dir = {}
        for p in raw_paths:
            parts = p.split("/")
            top = parts[0]
            is_dir.setdefault(top, len(parts) > 1)
        # 单顶层
        if len(tops) == 1:
            top = tops[0]
            if is_dir.get(top, False):
                return top          # 单目录：显示目录名
            return "/" + top        # 单文件：显示 /文件名
        # 多个顶层
        shown = "，".join(tops[:3])
        if len(tops) > 3:
            shown += f" 等{len(tops)}项"
        return shown

    # ------------------------------------------------------------------
    # 分卷删除
    # ------------------------------------------------------------------    # ------------------------------------------------------------------
    # 分卷删除
    # ------------------------------------------------------------------
    def delete_archive_with_parts(self) -> None:
        """
        删除压缩包及其全部分卷文件。

        分卷命名常见三种：
          - 现代 RAR/7z：  A.part1.rar, A.part2.rar, ...（partN.扩展名）
          - 7-Zip 分卷：    A.7z.001, A.7z.002, ...（.7z.NNN）
          - 老式 RAR：      A.rar + A.r00, A.r01, ...（part1 本身是 .rar）
        解压成功后删除源文件时调用，避免只删触发分卷而残留其他分卷。
        """
        try:
            d = os.path.dirname(os.path.abspath(self.archive_path))
            if not os.path.isdir(d):
                return
            base = os.path.basename(self.archive_path)
            patterns = []

            # 1) A.partN.ext （part + 数字 + 扩展名）
            m = re.match(r"^(.*\.part)\d+(\.[^.]+)$", base)
            if m:
                prefix, ext = m.group(1), m.group(2)
                patterns.append(re.compile(
                    "^" + re.escape(prefix) + r"\d+" + re.escape(ext) + "$"))

            # 2) A.7z.NNN  （.7z. + 数字）
            m2 = re.match(r"^(.*\.7z\.)\d+$", base)
            if m2:
                prefix = m2.group(1)
                patterns.append(re.compile("^" + re.escape(prefix) + r"\d+$"))

            # 3) 老式 RAR：A.rar + A.r00 分卷（part1 本身是 .rar）
            m3 = re.match(r"^(.*)\.rar$", base)
            if m3:
                stem = m3.group(1)
                if os.path.exists(os.path.join(d, stem + ".r00")):
                    patterns.append(re.compile(
                        "^" + re.escape(stem) + r"\.r\d+$"))
                    patterns.append(re.compile("^" + re.escape(stem) + r"\.rar$"))

            # 匹配到任何模式的同组文件全部删除
            removed = []
            for name in os.listdir(d):
                if any(p.match(name) for p in patterns):
                    try:
                        os.remove(os.path.join(d, name))
                        removed.append(name)
                    except OSError:
                        pass
            # 一个分卷模式都没匹配到时，退化为删除自身
            if not removed:
                try:
                    os.remove(self.archive_path)
                except OSError:
                    pass
        except Exception:
            pass  # 删除失败不影响主流程

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


# GBK 误读 Shift-JIS 数据时常见的“乱码特征字”（生僻/罕用，几乎不会出现在真实文件名）
_MOJIBAKE_HANZI = set(
    "戞榖怴斣慻僼僅儖僟掞摎搤摯撉撏撉揱揹擯攃攄攞攠攡攣攦"
    "攩攪攭攮攱攲攳攴攵攷攺攼攽政扂扃扄扆"
)


def _detect_name_encoding(raw: bytes) -> str:
    """
    探测 ZIP 文件名字节的最佳解码编码。

    GBK 与 Shift-JIS 都是宽松双字节编码，单凭字节合法性无法可靠区分。
    采用“乱码特征”双向启发式：
      - UTF-8 严格解码成功且含非 ASCII → utf-8
      - 半角片假名（U+FF65..FF9F）出现 → SJIS 误读 GBK 数据的特征 → gbk
      - “乱码特征字”出现（GBK 误读 SJIS 数据的生僻字产物）→ shift_jis
      - 否则按中文字符占比回退 gbk
    """
    try:
        dec = raw.decode("utf-8")
        if any(ord(c) > 127 for c in dec):
            return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        g = raw.decode("gbk")
    except UnicodeDecodeError:
        g = None
    try:
        s = raw.decode("shift_jis")
    except UnicodeDecodeError:
        s = None
    if g is None and s is None:
        return "gbk"

    # 强信号：半角片假名（SJIS 误读 GBK 字节的特征）
    if g is not None and any(0xFF65 <= ord(c) <= 0xFF9F for c in g):
        return "gbk"
    # 强信号：乱码特征字（GBK 误读 SJIS 字节的生僻字产物）
    if g is not None and any(c in _MOJIBAKE_HANZI for c in g):
        return "shift_jis"
    if g is not None and s is not None:
        # 弱信号：中文常用字占比
        han_g = sum(1 for c in g if 0x4E00 <= ord(c) <= 0x9FA5)
        han_s = sum(1 for c in s if 0x4E00 <= ord(c) <= 0x9FA5)
        return "gbk" if han_g >= han_s else "shift_jis"
    return "gbk" if g is not None else "shift_jis"


def _zip_central_entries(path: str) -> list:
    """
    读取 ZIP 中央目录，返回 [(相对路径, 原始文件名字节), ...]。
    适用于 ZIP 系容器（zip/jar/apk/docx/xlsx/pptx/epub 均为 ZIP 结构）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    entries = []
    i = 0
    while True:
        i = data.find(b"PK\x01\x02", i)
        if i < 0:
            break
        # 中央目录头固定 46 字节；文件名长度在 offset 28（2字节）
        nlen = int.from_bytes(data[i + 28:i + 30], "little")
        elen = int.from_bytes(data[i + 30:i + 32], "little")
        clen = int.from_bytes(data[i + 32:i + 34], "little")
        fn = data[i + 46:i + 46 + nlen]
        # 跳过目录条目（以 / 结尾）与空名
        if fn and not fn.endswith(b"/"):
            entries.append((fn, fn))
        i += 46 + nlen + elen + clen
    return entries


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