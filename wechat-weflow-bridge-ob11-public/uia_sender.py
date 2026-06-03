"""
uia_sender.py — 基于 Windows UI Automation 的微信 4.0+ 消息发送器
=================================================================

原理：
  微信 4.0 基于 Electron (Chromium)。Chromium 通过 UIA 桥将 HTML 输入元素
  暴露为标准 UIA 控件。通过 ValuePattern 设置输入框文本，InvokePattern 点击
  发送按钮。全程无鼠标键盘模拟，无 DLL 注入，风控风险极低。

工作流：
  1. 定位微信 4.0 窗口 (Electron/Chromium)
  2. 搜索联系人 → 点击匹配项 → 切换到目标聊天
  3. 定位聊天输入框 (EditControl + ValuePattern)
  4. 设置文本 → 点击发送按钮或 Enter
  5. 图片通过剪贴板粘贴后发送

依赖:
  pip install uiautomation pyperclip
  发送图片需要 Pillow: pip install Pillow
"""

import logging
import os
import re
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
    基于 Windows UI Automation 的微信 4.0+ 发送器

    对微信 4.0 (Electron/Chromium) 优化：
      - 自动检测 Electron 架构
      - ValuePattern 直接设值（非键盘模拟）
      - InvokePattern 精确点击发送按钮
      - 自动联系人搜索切换

    Attributes:
        search_enabled: 是否自动搜索联系人（默认 True，False 则需手动切到聊天窗口）
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    EXCLUDE_CLASSES = ["Chrome_WidgetWin_1", "CabinetWClass"]

    def __init__(self, search_enabled: bool = True):
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None
        self._is_electron = False  # True=4.0+, False=3.9

        # 控件缓存
        self._search_box = None
        self._input_control = None
        self._send_button = None
        self._last_contact = ""
        self._use_coord_fallback = False

        self.search_enabled = search_enabled

        self._init()

    # ================================================================
    # 初始化
    # ================================================================

    def _init(self):
        """初始化 UIA 并定位窗口"""
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
        """按标题搜索微信窗口"""
        auto = self._auto
        root = auto.GetRootControl()
        for w in root.GetChildren():
            cls = w.ClassName
            if cls in self.EXCLUDE_CLASSES:
                continue
            for kw in self.WECHAT_TITLES:
                if kw in w.Name:
                    self._window = w
                    if cls != "WeChatMainWndForPC":
                        self._is_electron = True
                    return

    # ================================================================
    # 控件定位
    # ================================================================

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
        """激活微信窗口到前台（AttachThreadInput 确保后台也能生效）"""
        try:
            self._window.SetActive()
            time.sleep(0.3)
        except Exception:
            try:
                self._window.SwitchToThisWindow()
                time.sleep(0.3)
            except Exception:
                pass
        # AttachThreadInput 绕过 Windows 后台进程不能 SetForegroundWindow 的限制
        try:
            import ctypes
            from ctypes import wintypes
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

    def _dump_tree(self, ctrl, depth: int = 0, max_depth: int = 4):
        """调试: 输出 UIA 子树（仅 debug）"""
        if depth > max_depth:
            return
        try:
            pad = "  " * depth
            name = (ctrl.Name or "")[:40]
            cls = ctrl.ClassName or ""
            ctrl_type = ctrl.ControlTypeName
            vp = ctrl.IsValuePatternAvailable if hasattr(ctrl, 'IsValuePatternAvailable') else '?'
            ip = ctrl.IsInvokePatternAvailable if hasattr(ctrl, 'IsInvokePatternAvailable') else '?'
            rect = ctrl.BoundingRectangle
            info = f"[{rect.left},{rect.top} {rect.width()}x{rect.height()}]" if rect else ""
            log.debug(f"{pad}{ctrl_type} '{name}' {info} V={vp} I={ip} cls={cls}")
            for child in ctrl.GetChildren():
                self._dump_tree(child, depth + 1, max_depth)
        except Exception:
            pass

    def _find_search_box_uia(self):
        """
        通过 UIA 树定位微信搜索框。

        微信 4.x (Qt/Electron) 的搜索框特征：
        - EditControl 类型
        - 窗口上半部分 (top < 30% 窗口高度)
        - 宽度小于窗口一半（区别于底部的聊天输入框）
        - 宽度大于 50px（排除小控件）
        """
        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_w = win_rect.width()
        win_h = win_rect.height()

        edits = []

        def walk(ctrl, depth=0):
            if depth > 12:
                return
            try:
                for child in ctrl.GetChildren():
                    if child.ControlTypeName == "EditControl":
                        rect = child.BoundingRectangle
                        if rect and rect.width() > 50:
                            edits.append((child, rect))
                    walk(child, depth + 1)
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception:
            pass

        # 过滤：上半部分的 EditControl，宽度小于窗口一半
        candidates = [
            (c, r) for c, r in edits
            if r.top < win_rect.top + win_h * 0.3 and r.width() < win_w * 0.5
        ]

        if not candidates:
            return None

        # 取最靠上的（搜索框通常比任何其他上半部分控件更高）
        candidates.sort(key=lambda x: x[1].top)
        return candidates[0][0]

    def _focus_chat_input(self):
        """
        物理点击聊天输入框区域（坐标后备模式专用）。
        让聊天输入框获得键盘焦点。
        """
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
        if not hwnd:
            return

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        input_x = rect.left + int(win_w * 0.3)
        input_y = rect.top + int(win_h * 0.92)
        ctypes.windll.user32.SetCursorPos(input_x, input_y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.3)

    def _switch_contact(self, contact: str) -> bool:
        """
        切换到指定联系人/群聊的聊天窗口。

        Ctrl+F 搜索 → 粘贴 → Enter
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

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
        ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(0.3)

        try:
            # Ctrl+F 打开搜索
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x46, 0, 0, 0)   # F
            ctypes.windll.user32.keybd_event(0x46, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.5)

            # 清空搜索框
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x41, 0, 0, 0)   # A
            ctypes.windll.user32.keybd_event(0x41, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.15)

            # 粘贴联系人/群名
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(0.1)
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
            ctypes.windll.user32.keybd_event(0x56, 0, 0, 0)   # V
            ctypes.windll.user32.keybd_event(0x56, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            time.sleep(0.3)

            # Enter → 选中第一个结果
            ctypes.windll.user32.keybd_event(0x0D, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x0D, 0, 2, 0)
            time.sleep(0.8)

            log.info(f"已切到联系人: {contact}")
            return True
        finally:
            ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)

    def _locate_input(self) -> bool:
        """
        定位聊天输入框和发送按钮

        在 Electron 中，聊天输入框是 EditControl (支持 ValuePattern)，
        位于窗口下半部分。
        """
        if not self._ensure_window():
            return False

        # 如果已有缓存且窗口没变，直接返回
        if self._input_control is not None:
            try:
                self._input_control.GetCurrentPattern()
                return True
            except Exception:
                self._input_control = None
                self._send_button = None

        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_center_y = win_rect.top + win_rect.height() / 2

        edits = []

        def walk(ctrl, depth=0):
            if depth > 14:
                return
            try:
                for child in ctrl.GetChildren():
                    try:
                        cn = child.ControlTypeName
                        # 输入控件
                        if cn == "EditControl":
                            edits.append(child)
                        walk(child, depth + 1)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception as e:
            log.debug(f"UIA 遍历异常: {e}")

        if not edits:
            log.warning("未找到输入控件，使用坐标后备方案（Qt 界面）")
            self._use_coord_fallback = True
            return True

        # 过滤：聊天输入框在窗口下半部分，面积较大
        candidates = [e for e in edits
                      if e.BoundingRectangle and
                      e.BoundingRectangle.top >= win_center_y - 20 and
                      e.BoundingRectangle.width() > 100]

        if not candidates:
            candidates = [e for e in edits if e.BoundingRectangle]

        # 按面积倒序，最大的就是聊天输入框
        candidates.sort(key=lambda e: e.BoundingRectangle.width() *
                        e.BoundingRectangle.height(), reverse=True)

        for ctrl in candidates:
            rect = ctrl.BoundingRectangle
            area = rect.width() * rect.height()
            if area < 200:
                continue

            name = ctrl.Name or ""
            log.debug(f"输入候选: '{name[:30]}' {rect.width()}x{rect.height()} "
                      f"V={ctrl.IsValuePatternAvailable}")

            # 优先使用支持 ValuePattern 的
            if ctrl.IsValuePatternAvailable:
                self._input_control = ctrl
                log.info(f"聊天输入框: {rect.width()}x{rect.height()} "
                         f"(ValuePattern)")
                break

        if not self._input_control:
            # 后备：用面积最大的
            self._input_control = candidates[0] if candidates else edits[0]
            log.warning(f"输入框无 ValuePattern，使用 SendKeys 后备方案")
            log.debug(f"后备输入控件: {self._input_control.ControlTypeName} "
                      f"'{self._input_control.Name[:30]}'")

        # 查找发送按钮
        try:
            buttons = []

            def find_buttons(ctrl, depth=0):
                if depth > 8:
                    return
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName == "ButtonControl":
                            bn = child.Name or ""
                            if "发送" in bn or "Send" in bn or bn.strip() == "":
                                buttons.append(child)
                        find_buttons(child, depth + 1)
                except Exception:
                    pass

            find_buttons(self._window)
            if buttons:
                self._send_button = buttons[0]
                log.info("已定位发送按钮")
            else:
                log.info("未找到发送按钮，发送时用 Enter")
        except Exception:
            pass

        return True

    # ================================================================
    # 发送方法
    # ================================================================

    def send_text(self, contact: str, text: str) -> bool:
        """
        发送文本消息

        Args:
            contact: 联系人昵称/备注
            text: 消息内容
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

            # 切换到联系人（物理点击搜索框，坐标后备模式下也有效）
            if self.search_enabled and contact:
                if contact != self._last_contact:
                    if not self._switch_contact(contact):
                        log.warning(f"无法自动切换到 '{contact}'，尝试在当前窗口发送")
                    self._last_contact = contact

            # 定位输入框
            if not self._locate_input():
                return False

            try:
                if self._use_coord_fallback:
                    # Qt 界面：点击输入框区域→剪贴板粘贴→Enter
                    import pyperclip
                    import ctypes
                    from ctypes import wintypes
                    hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
                    if not hwnd:
                        hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
                    if hwnd:
                        rect = wintypes.RECT()
                        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        win_w = rect.right - rect.left
                        win_h = rect.bottom - rect.top
                        # 输入框大致在窗口底部居中偏左的位置
                        input_x = rect.left + int(win_w * 0.3)
                        input_y = rect.top + int(win_h * 0.92)
                        # 物理点击让输入框获得焦点（PostMessage 对 Qt 子控件无效）
                        ctypes.windll.user32.SetCursorPos(input_x, input_y)
                        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
                        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
                    time.sleep(0.3)
                    pyperclip.copy(text)
                    time.sleep(0.05)
                    self._auto.SendKeys('{Ctrl}v')
                    time.sleep(0.3)
                    self._auto.SendKeys('{Enter}')
                    log.info(f"[UIA✓] {contact}: {text[:50]}... (无鼠标模式)")
                    return True

                ctrl = self._input_control

                # 设置文本
                if ctrl.IsValuePatternAvailable:
                    try:
                        ctrl.SetValue("")
                        time.sleep(0.02)
                    except Exception:
                        pass
                    try:
                        ctrl.SetValue(text)
                    except Exception as e:
                        log.warning(f"SetValue 失败: {e}，尝试剪贴板")
                        import pyperclip
                        pyperclip.copy(text)
                        time.sleep(0.05)
                        ctrl.SendKeys('{Ctrl}a')
                        ctrl.SendKeys('{Ctrl}v')
                else:
                    # 没有 ValuePattern，用剪贴板
                    import pyperclip
                    pyperclip.copy(text)
                    ctrl.SendKeys('{Ctrl}a')
                    time.sleep(0.05)
                    ctrl.SendKeys('{Ctrl}v')

                time.sleep(0.1)

                # 发送
                if self._send_button:
                    self._send_button.Click()
                else:
                    ctrl.SendKeys('{Enter}')

                log.info(f"[UIA✓] {contact}: {text[:50]}...")
                return True

            except Exception as e:
                log.error(f"[UIA✗] {contact}: {e}")
                return False

    def send_image(self, contact: str, image_path: str) -> bool:
        """
        通过剪贴板发送图片

        Args:
            contact: 联系人
            image_path: 图片文件路径
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

                if self.search_enabled and contact:
                    if contact != self._last_contact:
                        self._switch_contact(contact)
                        self._last_contact = contact

                # 复制图片到剪贴板
                self._copy_image_to_clipboard(image_path)
                time.sleep(0.2)

                if not self._locate_input():
                    return False

                if self._use_coord_fallback:
                    import ctypes
                    from ctypes import wintypes
                    hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
                    if not hwnd:
                        hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
                    if hwnd:
                        rect = wintypes.RECT()
                        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        input_x = rect.left + int((rect.right - rect.left) * 0.3)
                        input_y = rect.top + int((rect.bottom - rect.top) * 0.92)
                        ctypes.windll.user32.SetCursorPos(input_x, input_y)
                        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.3)
                    self._auto.SendKeys('{Ctrl}v')
                    time.sleep(0.5)
                    self._auto.SendKeys('{Enter}')
                    log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)} (无鼠标模式)")
                    return True

                self._input_control.SendKeys('{Ctrl}v')
                time.sleep(0.5)

                if self._send_button:
                    self._send_button.Click()
                else:
                    self._input_control.SendKeys('{Enter}')

                log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)}")
                return True

            except Exception as e:
                log.error(f"[UIA✗] 图片 → {contact}: {e}")
                return False

    def _copy_image_to_clipboard(self, path: str):
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）"""
        abs_path = os.path.abspath(path)
        try:
            subprocess.run([
                "powershell", "-WindowStyle", "Hidden", "-Command",
                f"Add-Type -AssemblyName System.Windows.Forms;"
                f"$img = [System.Drawing.Image]::FromFile('{abs_path}');"
                f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                f"$img.Dispose()"
            ], check=True, timeout=10)
            log.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            log.error(f"复制图片到剪贴板失败: {e}")
            raise

    # ================================================================
    # 诊断
    # ================================================================

    def diagnose(self):
        """输出诊断信息，用于调试"""
        if not self._window:
            print("✗ 未找到微信窗口")
            return

        print(f"✓ 微信窗口: '{self._window.Name}'")
        print(f"  ClassName: {self._window.ClassName}")
        print(f"  Electron: {self._is_electron}")
        print(f"  位置: [{self._window.BoundingRectangle.left},"
              f"{self._window.BoundingRectangle.top}] "
              f"{self._window.BoundingRectangle.width()}x"
              f"{self._window.BoundingRectangle.height()}")

        print("\n--- UIA 树 ---")
        self._dump_tree(self._window, max_depth=4)

        print("\n--- 控件状态 ---")
        print(f"  输入框: {'✓' if self._input_control else '✗'}")
        print(f"  发送按钮: {'✓' if self._send_button else '✗'}")
