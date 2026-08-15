"""
uia_sender.py — 纯键盘模拟的微信消息发送器
=================================================================

原理：
  完全通过键盘快捷键操作微信，无鼠标/无 UIA ValuePattern。
  剪贴板 (pyperclip) + SendKeys 完成一切操作。

工作流：
  1. 定位微信窗口并激活到前台
  2. Ctrl+F 搜索联系人 → Enter 进入聊天
  3. 剪贴板复制消息 → Ctrl+V 粘贴 → Enter 发送
  4. 图片通过 PowerShell 复制到剪贴板 → Ctrl+V → Enter

依赖:
  pip install uiautomation pyperclip
  发送图片需要 PowerShell (Windows 自带)
"""

import logging
import os
import random
import subprocess
import threading
import time

log = logging.getLogger("weflow-bridge")


class BaseSender:
    """消息发送器基类"""
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError

    def send_image(self, contact: str, image_path: str) -> bool:
        raise NotImplementedError


class UiaSender(BaseSender):
    """
    纯键盘模拟的微信消息发送器。

    不依赖 UIA ValuePattern / InvokePattern / 鼠标点击，
    全程使用剪贴板 + SendKeys 键盘快捷键操作。
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    def __init__(self, search_enabled: bool = True):
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None

        # 最近联系人缓存（相同目标跳过搜索，快速发送）
        self._last_contact = ""

        self.search_enabled = search_enabled

        self._init()

    # ================================================================
    # 初始化
    # ================================================================

    def _init(self):
        """初始化 uiautomation 并定位微信窗口"""
        try:
            import uiautomation as auto
            self._auto = auto
        except ImportError:
            log.error("请先安装 uiautomation: pip install uiautomation")
            return

        log.info("正在搜索微信窗口...")
        self._find_window()
        if self._window:
            log.info(f"微信窗口: '{self._window.Name}' ClassName={self._window.ClassName}")
            self._ready = True

    def _find_window(self):
        """按标题/类名搜索微信窗口"""
        auto = self._auto
        root = auto.GetRootControl()
        for w in root.GetChildren():
            if w.ClassName in ("Chrome_WidgetWin_1", "CabinetWClass"):
                continue
            # 标题精确匹配，避免误匹配任何标题含“微信”字样的窗口
            if w.Name in self.WECHAT_TITLES:
                self._window = w
                return
            # 微信主窗口类名匹配（兼容标题变化）
            if w.ClassName in ("Qt51514QWindowIcon", "WeChatMainWndForPC"):
                self._window = w
                return

    def _ensure_window(self) -> bool:
        """确保窗口可用"""
        if not self._ready:
            return False
        if self._window and self._window.Exists(0.2):
            return True
        self._find_window()
        if not self._window:
            log.warning("微信窗口未找到")
            self._ready = False
            return False
        return True

    def _activate(self):
        """激活微信窗口到前台"""
        try:
            self._window.SetActive()
            time.sleep(0.3)
        except Exception:
            try:
                self._window.SwitchToThisWindow()
                time.sleep(0.3)
            except Exception:
                pass
        # AttachThreadInput 绕过 Windows 后台窗口激活限制
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
            if not hwnd:
                hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
            if hwnd:
                WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
                CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.BringWindowToTop(hwnd)
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)
        except Exception:
            pass

    # ================================================================
    # 联系人切换
    # ================================================================

    def _switch_contact(self, contact: str) -> bool:
        """
        切换到指定联系人/群聊的聊天窗口。

        纯键盘：Ctrl+F → 粘贴联系人名 → Enter
        """
        if not self._ensure_window():
            return False
        self._activate()

        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return False

        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
        if not hwnd:
            log.warning("找不到微信主窗口句柄")
            return False

        WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(0.3)

        try:
            auto = self._auto

            # Ctrl+F 打开搜索
            auto.SendKeys('{Ctrl}f')
            time.sleep(0.5)

            # Ctrl+A 全选 → 清空已有内容
            auto.SendKeys('{Ctrl}a')
            time.sleep(0.15)

            # 粘贴联系人名
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(0.1)
            auto.SendKeys('{Ctrl}v')
            time.sleep(0.3)

            # Enter 选中第一个结果
            auto.SendKeys('{Enter}')
            time.sleep(0.8)

            log.info(f"已切到联系人: {contact}")
            return True
        finally:
            ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)

    # ================================================================
    # 发送文字 (纯键盘)
    # ================================================================

    def send_text(self, contact: str, text: str) -> bool:
        """
        发送文本消息。

        纯键盘方案：剪贴板 → Ctrl+V → Enter
        """
        with self._lock:
            if not self._ready:
                log.error("UIA Sender 未就绪")
                return False

            if not self._ensure_window():
                return False

            # 安全检查：过滤 PIL 引用
            if "<PIL." in text or "PIL." in text:
                log.warning(f"跳过 PIL 引用消息: {text[:60]}")
                return False

            self._activate()
            auto = self._auto

            # 切换到联系人
            if self.search_enabled and contact:
                if contact != self._last_contact:
                    if not self._switch_contact(contact):
                        log.error(f"切换联系人失败，中止发送: {contact}")
                        return False
                    self._last_contact = contact

            try:
                import pyperclip

                # 模拟真人随机延时
                time.sleep(random.uniform(0.3, 1.0))

                # 复制消息到剪贴板
                pyperclip.copy(text)
                time.sleep(random.uniform(0.1, 0.3))

                # Ctrl+V 粘贴
                auto.SendKeys('{Ctrl}v')

                # 根据消息长度动态等待粘贴完成（长文本多等一会）
                paste_wait = min(len(text) * 0.02, 2.0) + random.uniform(0.3, 0.8)
                time.sleep(paste_wait)

                # Enter 发送
                auto.SendKeys('{Enter}')

                log.info(f"[UIA✓] {contact}: {text[:50]}...")
                return True

            except Exception as e:
                log.error(f"[UIA✗] {contact}: {e}")
                return False

    # ================================================================
    # 发送图片 (剪贴板 + 纯键盘)
    # ================================================================

    def send_image(self, contact: str, image_path: str) -> bool:
        """
        发送图片。

        方案：PowerShell 复制图片到剪贴板 → Ctrl+V → Enter
        """
        with self._lock:
            if not self._ready:
                return False
            if not os.path.isfile(image_path):
                log.error(f"图片不存在: {image_path}")
                return False

            try:
                if not self._ensure_window():
                    return False
                self._activate()
                auto = self._auto

                if self.search_enabled and contact:
                    if contact != self._last_contact:
                        if not self._switch_contact(contact):
                            log.error(f"切换联系人失败，中止发送: {contact}")
                            return False
                        self._last_contact = contact

                time.sleep(random.uniform(0.3, 0.8))

                # PowerShell 复制图片到剪贴板
                self._copy_image_to_clipboard(image_path)
                time.sleep(0.3)

                # Ctrl+V 粘贴
                auto.SendKeys('{Ctrl}v')
                time.sleep(random.uniform(0.8, 1.5))  # 等待微信加载图片预览

                # Enter 发送
                auto.SendKeys('{Enter}')

                log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)}")
                return True

            except Exception as e:
                log.error(f"[UIA✗] 图片 → {contact}: {e}")
                return False

    def _copy_image_to_clipboard(self, path: str):
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）"""
        abs_path = os.path.abspath(path)
        # PowerShell 单引号字符串内需将 ' 转义为 ''，防止路径含单引号破坏命令/注入
        ps_path = abs_path.replace("'", "''")
        try:
            subprocess.run([
                "powershell", "-WindowStyle", "Hidden", "-Command",
                f"Add-Type -AssemblyName System.Windows.Forms;"
                f"Add-Type -AssemblyName System.Drawing;"
                f"$img = [System.Drawing.Image]::FromFile('{ps_path}');"
                f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                f"$img.Dispose()"
            ], check=True, timeout=10)
            log.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            log.error(f"复制图片到剪贴板失败: {e}")
            raise
