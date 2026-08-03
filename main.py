# -*- coding: utf-8 -*-
"""
main.py — 7z 密码自动解压工具（tkinter 图形界面）

功能：
  - 选择压缩包、7z.exe 路径（可自动检测）、解压目标目录
  - 密码单：手动输入多行 或 从文本文件导入（UTF-8/GBK 自动识别）
  - 后台线程逐个尝试密码，实时刷新进度，支持中途取消
  - 结束后弹窗显示结果并生成 unlock_log.txt

线程模型：解压逻辑在 threading.Thread 中执行，
UI 更新通过 queue.Queue + root.after() 轮询完成（避免线程不安全 API）。
"""

import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

import contextmenu
import unlocker
from unlocker import (ZipUnlocker, load_password_list,
                        get_default_password_file, save_default_password_list)

APP_TITLE = "7z密码单解压助手"


class App:
    """主窗口类。"""

    def __init__(self, root: tk.Tk, archive_path: str | None = None):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("820x640")
        root.minsize(720, 560)
        self._launch_archive = archive_path if archive_path and os.path.isfile(archive_path) else None

        # UI 更新消息队列（后台线程 -> 主线程）
        self.ui_queue: queue.Queue = queue.Queue()

        # 后台任务状态
        self.worker: threading.Thread | None = None
        self.cancel_event: threading.Event | None = None
        self.running = False

        self._build_layout()
        self._auto_fill()
        self._poll_queue()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        """构建全部控件。"""
        pad = {"padx": 6, "pady": 3}

        # ---------- 顶部：文件选择区 ----------
        top = ttk.LabelFrame(self.root, text="文件设置", padding=8)
        top.pack(fill="x", padx=8, pady=6)

        # 压缩包
        row1 = ttk.Frame(top)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="压缩包文件：", width=12).pack(side="left")
        self.var_archive = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_archive).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row1, text="浏览...", width=8,
                   command=self._browse_archive).pack(side="left", padx=4)

        # 7z.exe 路径
        row2 = ttk.Frame(top)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="7z.exe 路径：", width=12).pack(side="left")
        self.var_7z = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_7z).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row2, text="自动检测", width=8,
                   command=self._auto_detect_7z).pack(side="left", padx=4)
        ttk.Button(row2, text="浏览...", width=8,
                   command=self._browse_7z).pack(side="left")

        # 解压目标目录
        row3 = ttk.Frame(top)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="解压到目录：", width=12).pack(side="left")
        self.var_dest = tk.StringVar()
        ttk.Entry(row3, textvariable=self.var_dest).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row3, text="浏览...", width=8,
                   command=self._browse_dest).pack(side="left", padx=4)

        # ---------- 中部：密码单 + 进度日志 ----------
        middle = ttk.PanedWindow(self.root, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=8, pady=4)

        # 左侧：密码单
        left = ttk.LabelFrame(middle, text="密码单（每行一个密码）", padding=6)
        self.txt_passwords = scrolledtext.ScrolledText(
            left, height=14, wrap="none", font=("Consolas", 10))
        self.txt_passwords.pack(fill="both", expand=True)

        left_btn_row = ttk.Frame(left)
        left_btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(left_btn_row, text="导入密码文件", width=12,
                   command=self._import_password_file).pack(side="left")
        ttk.Button(left_btn_row, text="保存密码单", width=11,
                   command=self._save_password_list).pack(side="left", padx=4)
        ttk.Button(left_btn_row, text="清空", width=8,
                   command=self._clear_passwords).pack(side="left", padx=4)

        # 右侧：进度日志（只读）
        right = ttk.LabelFrame(middle, text="尝试进度", padding=6)
        self.txt_log = scrolledtext.ScrolledText(
            right, height=14, wrap="none", state="disabled",
            font=("Consolas", 10))
        self.txt_log.pack(fill="both", expand=True)

        middle.add(left, weight=1)
        middle.add(right, weight=1)

        # ---------- 底部：进度条 + 按钮 ----------
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=6)

        self.lbl_status = ttk.Label(bottom, text="就绪", anchor="w")
        self.lbl_status.pack(fill="x")

        self.progress = ttk.Progressbar(bottom, maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=4)

        btn_row = ttk.Frame(bottom)
        btn_row.pack(fill="x")
        self.btn_start = ttk.Button(btn_row, text="开始尝试", width=14,
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(btn_row, text="取消", width=10,
                                     command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)

        # ---------- 右键菜单设置区 ----------
        ctx = ttk.LabelFrame(self.root, text="右键菜单（安装后在资源管理器右键压缩包 → 使用密码单解压…）",
                             padding=8)
        ctx.pack(fill="x", padx=8, pady=(0, 6))
        ctx_row = ttk.Frame(ctx)
        ctx_row.pack(fill="x")
        self.btn_ctx_install = ttk.Button(ctx_row, text="安装右键菜单", width=14,
                                          command=self._install_context_menu)
        self.btn_ctx_install.pack(side="left")
        self.btn_ctx_uninstall = ttk.Button(ctx_row, text="卸载右键菜单", width=14,
                                            command=self._uninstall_context_menu)
        self.btn_ctx_uninstall.pack(side="left", padx=6)
        self.lbl_ctx = ttk.Label(ctx_row, text="", anchor="w")
        self.lbl_ctx.pack(side="left", padx=8, fill="x", expand=True)
        self._refresh_ctx_status()

    # ------------------------------------------------------------------
    # 初始填充
    # ------------------------------------------------------------------
    def _auto_fill(self) -> None:
        """自动检测 7z.exe 并填入路径；若带参数启动则预填压缩包。"""
        found = ZipUnlocker.find_7z()
        if found:
            self.var_7z.set(found)
        else:
            self._log("警告：未自动检测到 7z.exe，请手动指定路径。")

        if self._launch_archive:
            self.var_archive.set(self._launch_archive)
            # 解压到压缩包所在目录（不额外建文件夹）
            self.var_dest.set(os.path.dirname(self._launch_archive))
            self._log(f"已载入压缩包：{self._launch_archive}")
            self._log("请导入或输入密码单，然后点击「开始尝试」。")

        # 加载永久维护的默认密码单
        self._load_default_passwords()

    def _load_default_passwords(self) -> None:
        """启动时自动加载 %APPDATA% 下的默认密码单（若存在）。"""
        path = get_default_password_file()
        if os.path.isfile(path):
            try:
                passwords = load_password_list(path)
            except Exception as e:  # noqa: BLE001 - 加载失败不阻塞
                self._log(f"加载默认密码单失败：{e}")
                return
            if passwords:
                self.txt_passwords.delete("1.0", "end")
                self.txt_passwords.insert("1.0", "\n".join(passwords) + "\n")
                self._log(f"已加载默认密码单（{len(passwords)} 个密码）：{path}")

    def _save_password_list(self) -> None:
        """把当前文本框中的密码保存为默认密码单（永久维护）。"""
        passwords = self._normalize_passwords(
            self.txt_passwords.get("1.0", "end"))
        if not passwords:
            messagebox.showwarning(APP_TITLE, "密码列表为空，没有可保存的内容。")
            return
        path = save_default_password_list(passwords)
        self._log(f"密码单已保存（{len(passwords)} 个密码）：{path}")
        messagebox.showinfo(
            APP_TITLE,
            f"密码单已保存，下次启动自动加载。\n\n"
            f"位置：{path}\n共 {len(passwords)} 个密码")

    # ------------------------------------------------------------------
    # 右键菜单设置
    # ------------------------------------------------------------------
    def _refresh_ctx_status(self) -> None:
        """刷新右键菜单安装状态显示。"""
        installed = contextmenu.is_installed()
        if installed:
            has_all = "*" in installed
            exts = [e for e in installed if e != "*"]
            txt = f"已安装（{len(exts)} 种扩展名" + (" + 所有文件" if has_all else "") + "）"
            self.lbl_ctx.config(text=txt)
        else:
            self.lbl_ctx.config(text="未安装")

    def _install_context_menu(self) -> None:
        res = contextmenu.install_menu()
        contextmenu._broadcast_refresh()
        if res["ok"]:
            self._refresh_ctx_status()
            has_all = "*" in res["installed"]
            exts = [e for e in res["installed"] if e != "*"]
            self._log(f"右键菜单安装成功：{len(exts)} 种扩展名"
                      + (" + 所有文件(任意后缀)" if has_all else ""))
            msg = (
                "右键菜单安装成功！\n\n"
                f"· 常见压缩格式 {len(exts)} 种：{', '.join(exts[:6])} 等\n"
                + ("· 所有文件（任意后缀名，防改后缀分享）\n" if has_all else "")
                + "\n现在右键任意文件 →「使用密码单解压…」即可。\n\n"
                "是否立即重启资源管理器，使菜单立刻生效？\n"
                "（重启后桌面/任务栏会闪一下，属正常现象）"
            )
            if messagebox.askyesno(APP_TITLE, msg):
                if contextmenu.restart_explorer():
                    self._log("已重启资源管理器，右键菜单已生效。")
                else:
                    self._log("资源管理器重启失败，请手动重启或按 F5 刷新。")
            else:
                self._log("未重启资源管理器；若右键看不到菜单，请重启资源管理器或注销。")
        else:
            messagebox.showerror(APP_TITLE, f"安装失败：\n{res['msg']}")

    def _uninstall_context_menu(self) -> None:
        res = contextmenu.uninstall_menu()
        contextmenu._broadcast_refresh()
        self._refresh_ctx_status()
        self._log(f"右键菜单已卸载：{len(res['removed'])} 个目标")
        if res["ok"]:
            msg = "右键菜单已卸载。" 
            if messagebox.askyesno(APP_TITLE, msg + "\n\n是否立即重启资源管理器使其生效？"):
                if contextmenu.restart_explorer():
                    self._log("已重启资源管理器。")
        else:
            messagebox.showerror(APP_TITLE, f"卸载失败：\n{res['msg']}")

    # ------------------------------------------------------------------
    # 浏览按钮
    # ------------------------------------------------------------------
    def _browse_archive(self) -> None:
        path = filedialog.askopenfilename(
            title="选择压缩包",
            filetypes=[("压缩文件",
                        "*.7z *.zip *.rar *.tar *.gz *.xz *.bz2 *.lzma *.cab "
                        "*.iso *.wim *.001"),
                       ("所有文件", "*.*")])
        if path:
            self.var_archive.set(path)
            # 默认解压目录：压缩包同名 + "_解压"
            self.var_dest.set(os.path.dirname(path))

    def _browse_7z(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 7z.exe", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.var_7z.set(path)

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(title="选择解压目标目录")
        if path:
            self.var_dest.set(path)

    def _auto_detect_7z(self) -> None:
        found = ZipUnlocker.find_7z()
        if found:
            self.var_7z.set(found)
            self._log(f"已自动检测到 7z.exe：{found}")
        else:
            messagebox.showwarning(APP_TITLE, "未自动检测到 7z.exe，请手动指定。")

    # ------------------------------------------------------------------
    # 密码单操作
    # ------------------------------------------------------------------
    def _import_password_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择密码文件",
            filetypes=[("文本文件", "*.txt *.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            passwords = load_password_list(path)
        except Exception as e:  # noqa: BLE001 - 展示给用户
            messagebox.showerror(APP_TITLE, f"读取密码文件失败：\n{e}")
            return
        if not passwords:
            messagebox.showwarning(APP_TITLE, "密码文件为空或没有有效密码行。")
            return
        self.txt_passwords.delete("1.0", "end")
        self.txt_passwords.insert("1.0", "\n".join(passwords) + "\n")
        self._log(f"已从 {os.path.basename(path)} 导入 {len(passwords)} 个密码。")

    def _clear_passwords(self) -> None:
        self.txt_passwords.delete("1.0", "end")

    # ------------------------------------------------------------------
    # 密码预处理（与 unlocker.load_password_list 同样的规则）
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_passwords(raw_text: str) -> list[str]:
        """按行拆分：去首尾空白、忽略空行、去重（保持首次出现顺序）。"""
        items: list[str] = []
        seen: set[str] = set()
        for line in raw_text.splitlines():
            p = line.strip()
            if not p:
                continue
            if p not in seen:
                seen.add(p)
                items.append(p)
        return items

    # ------------------------------------------------------------------
    # 开始 / 取消
    # ------------------------------------------------------------------
    def _start(self) -> None:
        """校验输入并启动后台解压线程。"""
        if self.running:
            return

        archive = self.var_archive.get().strip()
        seven_zip = self.var_7z.get().strip()
        dest = self.var_dest.get().strip()

        if not archive:
            messagebox.showwarning(APP_TITLE, "请先选择压缩包文件。")
            return
        if not os.path.isfile(archive):
            messagebox.showwarning(APP_TITLE, "压缩包文件不存在，请重新选择。")
            return
        if not seven_zip or not os.path.isfile(seven_zip):
            messagebox.showwarning(APP_TITLE, "7z.exe 路径无效，请指定或自动检测。")
            return
        if not dest:
            # 默认：解压到压缩包所在目录（不额外建文件夹）
            dest = os.path.dirname(archive)
            self.var_dest.set(dest)

        passwords = self._normalize_passwords(
            self.txt_passwords.get("1.0", "end"))
        if not passwords:
            messagebox.showwarning(APP_TITLE, "密码列表为空，请先输入或导入密码。")
            return

        # 进入运行状态
        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.progress["value"] = 0
        self.progress["maximum"] = len(passwords)
        self.lbl_status.config(text=f"开始尝试，共 {len(passwords)} 个密码...")
        self._log("=" * 40)
        self._log(f"开始时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"压缩包：{archive}")
        self._log(f"目标目录：{dest}")
        self._log(f"待尝试密码数：{len(passwords)}")
        self._log("=" * 40)

        unlocker_obj = ZipUnlocker(seven_zip, archive, dest)
        self.cancel_event = threading.Event()

        def worker() -> None:
            """后台线程：执行密码尝试，结果通过队列回传主线程。"""
            try:
                result = unlocker_obj.try_passwords(
                    passwords,
                    progress_cb=self._progress_cb,
                    cancel_event=self.cancel_event)
                self.ui_queue.put(("done", result, None))
            except Exception as e:  # noqa: BLE001 - 异常也要回传
                self.ui_queue.put(("done", None, str(e)))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _progress_cb(self, index: int, total: int, password: str,
                     status: str, detail: str, elapsed_ms: int) -> None:
        """后台线程回调：仅把消息放入队列，由主线程刷新 UI。"""
        self.ui_queue.put(("progress", (index, total, password,
                                        status, detail, elapsed_ms), None))

    def _cancel(self) -> None:
        """点击取消：置位取消事件（后台线程会终止当前 7z 进程并停止）。"""
        if not self.running:
            return
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.lbl_status.config(text="正在取消...")
        self._log("用户请求取消，正在终止当前解压...")

    # ------------------------------------------------------------------
    # UI 轮询（queue + after）
    # ------------------------------------------------------------------
    def _poll_queue(self) -> None:
        """每 100ms 检查一次队列并刷新 UI。"""
        try:
            while True:
                kind, payload, error = self.ui_queue.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)
                elif kind == "done":
                    self._handle_done(payload, error)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_progress(self, payload) -> None:
        """处理单次密码尝试的进度消息。"""
        index, total, password, status, detail, elapsed_ms = payload
        elapsed_s = elapsed_ms / 1000.0
        if status == "running":
            self.lbl_status.config(text=f"正在尝试 ({index}/{total})：{password}")
        else:
            mark = "✓" if status == "success" else "✗"
            self._log(f"[{index}/{total}] {password} → {detail} "
                      f"（{elapsed_s:.2f} 秒）{mark}")
            self.progress["value"] = index
            if status == "success":
                self.lbl_status.config(text=f"成功！密码：{password}")
            else:
                self.lbl_status.config(
                    text=f"已尝试 {index}/{total}，继续...")

    def _handle_done(self, result, error: str | None) -> None:
        """处理最终结果消息。"""
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.cancel_event = None

        if error:
            self._log(f"发生错误：{error}")
            messagebox.showerror(APP_TITLE, f"发生错误：\n{error}")
            return

        if result is None:
            self._log("任务结束（无结果）。")
            return

        self.progress["value"] = result["total"] or 0
        if result["success"]:
            self._log("")
            self._log(f"★ 解压成功！正确密码：{result['password']}")
            self.lbl_status.config(text=f"解压成功，密码：{result['password']}")
            messagebox.showinfo(
                APP_TITLE,
                f"解压成功！\n\n正确密码：{result['password']}\n"
                f"尝试次数：{result['attempted']} / {result['total']}")
        else:
            self._log("")
            self._log(f"全部失败，共尝试 {result['attempted']} 个密码。")
            self.lbl_status.config(text="解压失败：未找到正确密码")
            messagebox.showwarning(
                APP_TITLE,
                f"未找到正确密码。\n\n已尝试：{result['attempted']} / "
                f"{result['total']} 个密码")

    # ------------------------------------------------------------------
    # 日志输出（只读区域）
    # ------------------------------------------------------------------
    def _log(self, text: str) -> None:
        """向右侧日志区域追加一行。"""
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    # ------------------------------------------------------------------
    # 关闭窗口
    # ------------------------------------------------------------------
    def on_close(self) -> None:
        """关闭窗口：若正在运行则先取消后台任务。"""
        if self.running:
            if not messagebox.askyesno(
                    APP_TITLE, "解压仍在进行中，确定要退出吗？"):
                return
            if self.cancel_event is not None:
                self.cancel_event.set()
            if self.worker is not None:
                # 等待线程结束（最多 3 秒）
                self.worker.join(timeout=3.0)
        self.root.destroy()


def _cli_action(action: str) -> int:
    """命令行动作（供自动化/高级用户使用，GUI 程序无 stdout，结果写入 exe 同目录 result 文件）。
    返回 0 表示成功。"""
    result_file = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                               "ctxmenu_result.txt")
    try:
        if action == "--install-menu":
            res = contextmenu.install_menu()
            contextmenu._broadcast_refresh()
            ok = res["ok"]
            msg = f"已安装 {len(res['installed'])} 种扩展名" + ("；错误：" + res["msg"] if res["msg"] else "")
        elif action == "--uninstall-menu":
            res = contextmenu.uninstall_menu()
            contextmenu._broadcast_refresh()
            ok = res["ok"]
            msg = f"已卸载 {len(res['removed'])} 种扩展名" + ("；错误：" + res["msg"] if res["msg"] else "")
        elif action == "--status":
            inst = contextmenu.is_installed()
            ok = True
            msg = "已安装: " + ", ".join(inst) if inst else "未安装"
        else:
            return 1
        with io.open(result_file, "w", encoding="utf-8") as f:
            f.write(msg)
        return 0 if ok else 1
    except Exception as e:
        try:
            with io.open(result_file, "w", encoding="utf-8") as f:
                f.write(f"ERROR: {e}")
        except Exception:
            pass
        return 1


def _auto_unlock(archive_path: str) -> None:
    """
    一键解压（右键菜单调用）：
      - 显示精简进度窗口（压缩包名 + 进度条 + 尝试计数）
      - 成功：进度条跑完，窗口自动消失，删除源压缩包，无弹窗
      - 失败：进度条停止，弹窗提示“未找到正确密码”，窗口关闭
    """
    root = tk.Tk()
    root.title("7z密码单解压助手 — 解压中")
    root.geometry("440x180")
    root.resizable(False, False)
    # 居中显示
    root.update_idletasks()
    x = (root.winfo_screenwidth() - 440) // 2
    y = (root.winfo_screenheight() - 180) // 2
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)  # 置顶，避免被其他窗口挡住

    # 控件
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="正在解压：", anchor="w").pack(fill="x")
    lbl_file = ttk.Label(frame, text=os.path.basename(archive_path),
                         anchor="w", font=("Microsoft YaHei", 10))
    lbl_file.pack(fill="x", pady=(0, 4))
    # 解压内容预览（解压出来会得到什么）
    lbl_content = ttk.Label(frame, text="解压内容：读取中...",
                            anchor="w", font=("Microsoft YaHei", 9),
                            foreground="#555555", wraplength=420)
    lbl_content.pack(fill="x", pady=(0, 6))
    self_progress = ttk.Progressbar(frame, maximum=100, mode="determinate")
    self_progress.pack(fill="x", pady=4)
    lbl_status = ttk.Label(frame, text="准备中...", anchor="w")
    lbl_status.pack(fill="x")

    # UI 更新队列（后台线程 -> 主线程）
    ui_queue: queue.Queue = queue.Queue()

    seven = ZipUnlocker.find_7z()
    if not seven:
        root.destroy()
        messagebox.showerror(APP_TITLE, "未找到 7z.exe，无法解压。")
        return

    pw_path = get_default_password_file()
    try:
        passwords = load_password_list(pw_path)
    except Exception as e:  # noqa: BLE001
        passwords = []
    if not passwords:
        root.destroy()
        messagebox.showwarning(
            APP_TITLE,
            "默认密码单为空，无法自动尝试。\n\n"
            f"请先打开本程序，在密码单中输入密码后点「保存密码单」。\n"
            f"（{pw_path}）")
        return

    dest = os.path.dirname(archive_path)  # 解压到压缩包所在目录，不额外建文件夹
    u = ZipUnlocker(seven, archive_path, dest)

    def progress_cb(index: int, total: int, password: str,
                    status: str, detail: str, elapsed_ms: int) -> None:
        ui_queue.put(("progress", (index, total, password, status, detail)))

    # 解压前检查重名冲突，询问用户如何处理
    overwrite = "overwrite"
    try:
        conflicts = u.check_conflicts()
    except Exception:
        conflicts = []
    if conflicts:
        shown = "、".join(conflicts[:5]) + (" 等" if len(conflicts) > 5 else "")
        choice = messagebox.askyesnocancel(
            APP_TITLE,
            f"目标目录已存在同名文件/文件夹：\n{shown}\n\n"
            f"共 {len(conflicts)} 个。\n\n"
            "「是」= 覆盖现有文件\n「否」= 跳过重名文件，解压其余\n"
            "「取消」= 中止本次解压")
        if choice is None:
            root.destroy()
            return
        overwrite = "overwrite" if choice else "skip"

    def worker() -> None:
        try:
            result = u.try_passwords(passwords, progress_cb=progress_cb,
                                     overwrite=overwrite)
        except Exception as e:  # noqa: BLE001
            result = {"success": False, "password": None,
                      "attempted": 0, "total": len(passwords),
                      "errors": [str(e)]}
        ui_queue.put(("done", result))

    def _poll_queue() -> None:
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "progress":
                    index, total, password, status, detail = payload
                    if status == "running":
                        lbl_status.config(text=f"正在尝试 {index}/{total}：{password}")
                        self_progress["maximum"] = total
                        self_progress["value"] = max(index - 1, 0)
                    else:
                        lbl_status.config(
                            text=f"已尝试 {index}/{total}：{password} → {detail}")
                        self_progress["value"] = index
                elif kind == "done":
                    _finish(payload)
        except queue.Empty:
            pass
        root.after(80, _poll_queue)

    def _finish(result) -> None:
        if result["success"]:
            # 成功：进度条满格，删除源压缩包（含分卷），窗口自动消失（无弹窗）
            self_progress["value"] = self_progress["maximum"]
            lbl_status.config(text="解压完成")
            root.update_idletasks()
            u.delete_archive_with_parts()  # 删除压缩包及全部分卷
            root.destroy()
        else:
            msg = (f"未能解压：{os.path.basename(archive_path)}\n\n"
                   f"已尝试 {result['attempted']} 个密码，均不正确或解压失败。\n"
                   f"可打开本程序，补充密码后点「保存密码单」再试。")
            root.destroy()
            messagebox.showwarning(APP_TITLE, msg)

    # 提前读取解压内容预览并填充（解压出来会得到什么）
    try:
        preview = u.get_extract_preview()
        if preview:
            lbl_content.config(text=f"解压内容：{preview}")
        else:
            lbl_content.config(text="解压内容：—")
    except Exception:
        lbl_content.config(text="解压内容：—")

    threading.Thread(target=worker, daemon=True).start()
    root.after(80, _poll_queue)
    root.mainloop()


def main() -> None:
    # 支持命令行参数：main.py [--auto] [压缩包路径]
    args = sys.argv[1:]
    # CLI 动作优先（不启动 GUI）
    if args and args[0] in ("--install-menu", "--uninstall-menu", "--status"):
        sys.exit(_cli_action(args[0]))

    archive_path = None
    if args:
        p = args[-1].strip().strip('"')
        if p and (p.lower().endswith(tuple(contextmenu.EXTENSIONS)) or os.path.isfile(p)):
            archive_path = p

    # 静默一键模式：右键菜单用 --auto 调用，自动解压后退出
    if archive_path and "--auto" in args:
        _auto_unlock(archive_path)
        return

    root = tk.Tk()
    app = App(root, archive_path=archive_path)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
