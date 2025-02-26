# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Wrapper over our (QtWebKit) WebView."""

import re
import functools
import xml.etree.ElementTree
from typing import cast, Optional
from collections.abc import Iterable

from qutebrowser.qt.core import pyqtSlot, Qt, QUrl, QPoint, QTimer, QSizeF, QSize
from qutebrowser.qt.gui import QIcon
from qutebrowser.qt.widgets import QWidget
# pylint: disable=no-name-in-module
from qutebrowser.qt.webkitwidgets import QWebPage, QWebFrame
from qutebrowser.qt.webkit import QWebSettings, QWebHistory, QWebElement
# pylint: enable=no-name-in-module
from qutebrowser.qt.printsupport import QPrinter

from qutebrowser.browser import browsertab, shared
from qutebrowser.browser.webkit import (webview, webpage, tabhistory, webkitelem,
                                        webkitsettings, webkitinspector)
from qutebrowser.browser.webkit.network import networkmanager
from qutebrowser.utils import qtutils, usertypes, utils, log, debug, resources
from qutebrowser.keyinput import modeman
from qutebrowser.qt import sip


class WebKitAction(browsertab.AbstractAction):

    """QtWebKit implementations related to web actions."""

    action_base = QWebPage.WebAction

    _widget: webview.WebView

    def exit_fullscreen(self):
        raise browsertab.UnsupportedOperationError

    def save_page(self):
        """Save the current page."""
        raise browsertab.UnsupportedOperationError

    def show_source(self, pygments=False):
        self._show_source_pygments()

    def run_string(self, name: str) -> None:
        """Add special cases for new API.

        Those were added to QtWebKit 5.212 (which we enforce), but we don't get
        the new API from PyQt. Thus, we'll need to use the raw numbers.
        """
        new_actions = {
            # https://github.com/qtwebkit/qtwebkit/commit/a96d9ef5d24b02d996ad14ff050d0e485c9ddc97
            'RequestClose': QWebPage.WebAction.ToggleVideoFullscreen + 1,
            # https://github.com/qtwebkit/qtwebkit/commit/96b9ba6269a5be44343635a7aaca4a153ea0366b
            'Unselect': QWebPage.WebAction.ToggleVideoFullscreen + 2,
        }
        if name in new_actions:
            self._widget.triggerPageAction(new_actions[name])  # type: ignore[arg-type]
            return

        super().run_string(name)


class WebKitPrinting(browsertab.AbstractPrinting):

    """QtWebKit implementations related to printing."""

    _widget: webview.WebView

    def check_pdf_support(self):
        pass

    def check_preview_support(self):
        pass

    def to_pdf(self, path):
        printer = QPrinter()
        printer.setOutputFileName(str(path))
        self._widget.print(printer)
        # Can't find out whether there was an error...
        self.pdf_printing_finished.emit(str(path), True)

    def to_printer(self, printer):
        self._widget.print(printer)
        # Can't find out whether there was an error...
        self.printing_finished.emit(True)


class WebKitSearch(browsertab.AbstractSearch):

    """QtWebKit implementations related to searching on the page."""

    _widget: webview.WebView

    def __init__(self, tab, parent=None):
        super().__init__(tab, parent)
        self._flags = self._empty_flags()

    def _empty_flags(self):
        return QWebPage.FindFlags(0)  # type: ignore[call-overload]

    def _args_to_flags(self, reverse, ignore_case):
        flags = self._empty_flags()
        if self._is_case_sensitive(ignore_case):
            flags |= QWebPage.FindFlag.FindCaseSensitively
        if reverse:
            flags |= QWebPage.FindFlag.FindBackward
        return flags

    def _call_cb(self, callback, found, text, flags, caller):
        """Call the given callback if it's non-None.

        Delays the call via a QTimer so the website is re-rendered in between.

        Args:
            callback: What to call
            found: If the text was found
            text: The text searched for
            flags: The flags searched with
            caller: Name of the caller.
        """
        found_text = 'found' if found else "didn't find"
        # Removing FindWrapsAroundDocument to get the same logging as with
        # QtWebEngine
        debug_flags = debug.qflags_key(
            QWebPage, flags & ~QWebPage.FindFlag.FindWrapsAroundDocument,
            klass=QWebPage.FindFlag)
        if debug_flags != '0x0000':
            flag_text = 'with flags {}'.format(debug_flags)
        else:
            flag_text = ''
        log.webview.debug(' '.join([caller, found_text, text, flag_text])
                          .strip())
        if callback is not None:
            if caller in ["prev_result", "next_result"]:
                if found:
                    # no wrapping detection
                    cb_value = browsertab.SearchNavigationResult.found
                elif flags & QWebPage.FindBackward:
                    cb_value = browsertab.SearchNavigationResult.wrap_prevented_top
                else:
                    cb_value = browsertab.SearchNavigationResult.wrap_prevented_bottom
            elif caller == "search":
                cb_value = found
            else:
                raise utils.Unreachable(caller)
            QTimer.singleShot(0, functools.partial(callback, cb_value))

        self.finished.emit(found)

    def clear(self):
        if self.search_displayed:
            self.cleared.emit()
        self.search_displayed = False
        # We first clear the marked text, then the highlights
        self._widget.findText('')
        self._widget.findText(
            '', QWebPage.FindFlag.HighlightAllOccurrences)  # type: ignore[arg-type]

    def search(self, text, *, ignore_case=usertypes.IgnoreCase.never,
               reverse=False, result_cb=None):
        # Don't go to next entry on duplicate search
        if self.text == text and self.search_displayed:
            log.webview.debug("Ignoring duplicate search request"
                              " for {}, but resetting flags".format(text))
            self._flags = self._args_to_flags(reverse, ignore_case)
            return

        # Clear old search results, this is done automatically on QtWebEngine.
        self.clear()

        self.text = text
        self.search_displayed = True
        self._flags = self._args_to_flags(reverse, ignore_case)
        # We actually search *twice* - once to highlight everything, then again
        # to get a mark so we can navigate.
        found = self._widget.findText(text, self._flags)
        self._widget.findText(text,
                              self._flags | QWebPage.FindFlag.HighlightAllOccurrences)
        self._call_cb(result_cb, found, text, self._flags, 'search')

    def next_result(self, *, wrap=False, callback=None):
        self.search_displayed = True
        # The int() here makes sure we get a copy of the flags.
        flags = QWebPage.FindFlags(
            int(self._flags))  # type: ignore[call-overload]

        if wrap:
            flags |= QWebPage.FindFlag.FindWrapsAroundDocument

        found = self._widget.findText(self.text, flags)  # type: ignore[arg-type]
        self._call_cb(callback, found, self.text, flags, 'next_result')

    def prev_result(self, *, wrap=False, callback=None):
        self.search_displayed = True
        # The int() here makes sure we get a copy of the flags.
        flags = QWebPage.FindFlags(
            int(self._flags))  # type: ignore[call-overload]

        if flags & QWebPage.FindFlag.FindBackward:
            flags &= ~QWebPage.FindFlag.FindBackward
        else:
            flags |= QWebPage.FindFlag.FindBackward

        if wrap:
            flags |= QWebPage.FindFlag.FindWrapsAroundDocument

        found = self._widget.findText(self.text, flags)  # type: ignore[arg-type]
        self._call_cb(callback, found, self.text, flags, 'prev_result')


class WebKitCaret(browsertab.AbstractCaret):

    """QtWebKit implementations related to moving the cursor/selection."""

    _widget: webview.WebView

    def __init__(self,
                 tab: 'WebKitTab',
                 mode_manager: modeman.ModeManager,
                 parent: QWidget = None) -> None:
        super().__init__(tab, mode_manager, parent)
        self._selection_state = browsertab.SelectionState.none

    @pyqtSlot(usertypes.KeyMode)
    def _on_mode_entered(self, mode):
        if mode != usertypes.KeyMode.caret:
            return

        if self._widget.hasSelection():
            self._selection_state = browsertab.SelectionState.normal
        else:
            self._selection_state = browsertab.SelectionState.none
        self.selection_toggled.emit(self._selection_state)
        settings = self._widget.settings()
        settings.setAttribute(QWebSettings.WebAttribute.CaretBrowsingEnabled, True)

        if self._widget.isVisible():
            # Sometimes the caret isn't immediately visible, but unfocusing
            # and refocusing it fixes that.
            self._widget.clearFocus()
            self._widget.setFocus(Qt.FocusReason.OtherFocusReason)

            # Move the caret to the first element in the viewport if there
            # isn't any text which is already selected.
            #
            # Note: We can't use hasSelection() here, as that's always
            # true in caret mode.
            if self._selection_state is browsertab.SelectionState.none:
                self._widget.page().currentFrame().evaluateJavaScript(
                    resources.read_file('javascript/position_caret.js'))

    @pyqtSlot(usertypes.KeyMode)
    def _on_mode_left(self, _mode):
        settings = self._widget.settings()
        if settings.testAttribute(QWebSettings.WebAttribute.CaretBrowsingEnabled):
            if (self._selection_state is not browsertab.SelectionState.none and
                    self._widget.hasSelection()):
                # Remove selection if it exists
                self._widget.triggerPageAction(QWebPage.WebAction.MoveToNextChar)
            settings.setAttribute(QWebSettings.WebAttribute.CaretBrowsingEnabled, False)
            self._selection_state = browsertab.SelectionState.none

    def move_to_next_line(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = QWebPage.WebAction.SelectNextLine
        else:
            act = QWebPage.WebAction.MoveToNextLine
        for _ in range(count):
            self._widget.triggerPageAction(act)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_end()

    def move_to_prev_line(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = QWebPage.WebAction.SelectPreviousLine
        else:
            act = QWebPage.WebAction.MoveToPreviousLine
        for _ in range(count):
            self._widget.triggerPageAction(act)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_start()

    def move_to_next_char(self, count=1):
        if self._selection_state is browsertab.SelectionState.normal:
            act = QWebPage.WebAction.SelectNextChar
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = QWebPage.WebAction.MoveToNextChar
        for _ in range(count):
            self._widget.triggerPageAction(act)

    def move_to_prev_char(self, count=1):
        if self._selection_state is browsertab.SelectionState.normal:
            act = QWebPage.WebAction.SelectPreviousChar
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = QWebPage.WebAction.MoveToPreviousChar
        for _ in range(count):
            self._widget.triggerPageAction(act)

    def move_to_end_of_word(self, count=1):
        if self._selection_state is browsertab.SelectionState.normal:
            act = [QWebPage.WebAction.SelectNextWord]
            if utils.is_windows:  # pragma: no cover
                act.append(QWebPage.WebAction.SelectPreviousChar)
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = [QWebPage.WebAction.MoveToNextWord]
            if utils.is_windows:  # pragma: no cover
                act.append(QWebPage.WebAction.MoveToPreviousChar)
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)

    def move_to_next_word(self, count=1):
        if self._selection_state is browsertab.SelectionState.normal:
            act = [QWebPage.WebAction.SelectNextWord]
            if not utils.is_windows:  # pragma: no branch
                act.append(QWebPage.WebAction.SelectNextChar)
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = [QWebPage.WebAction.MoveToNextWord]
            if not utils.is_windows:  # pragma: no branch
                act.append(QWebPage.WebAction.MoveToNextChar)
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)

    def move_to_prev_word(self, count=1):
        if self._selection_state is browsertab.SelectionState.normal:
            act = QWebPage.WebAction.SelectPreviousWord
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = QWebPage.WebAction.MoveToPreviousWord
        for _ in range(count):
            self._widget.triggerPageAction(act)

    def move_to_start_of_line(self):
        if self._selection_state is browsertab.SelectionState.normal:
            act = QWebPage.WebAction.SelectStartOfLine
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = QWebPage.WebAction.MoveToStartOfLine
        self._widget.triggerPageAction(act)

    def move_to_end_of_line(self):
        if self._selection_state is browsertab.SelectionState.normal:
            act = QWebPage.WebAction.SelectEndOfLine
        elif self._selection_state is browsertab.SelectionState.line:
            return
        else:
            act = QWebPage.WebAction.MoveToEndOfLine
        self._widget.triggerPageAction(act)

    def move_to_start_of_next_block(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = [QWebPage.WebAction.SelectNextLine,
                   QWebPage.WebAction.SelectStartOfBlock]
        else:
            act = [QWebPage.WebAction.MoveToNextLine,
                   QWebPage.WebAction.MoveToStartOfBlock]
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_end()

    def move_to_start_of_prev_block(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = [QWebPage.WebAction.SelectPreviousLine,
                   QWebPage.WebAction.SelectStartOfBlock]
        else:
            act = [QWebPage.WebAction.MoveToPreviousLine,
                   QWebPage.WebAction.MoveToStartOfBlock]
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_start()

    def move_to_end_of_next_block(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = [QWebPage.WebAction.SelectNextLine,
                   QWebPage.WebAction.SelectEndOfBlock]
        else:
            act = [QWebPage.WebAction.MoveToNextLine,
                   QWebPage.WebAction.MoveToEndOfBlock]
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_end()

    def move_to_end_of_prev_block(self, count=1):
        if self._selection_state is not browsertab.SelectionState.none:
            act = [QWebPage.WebAction.SelectPreviousLine, QWebPage.WebAction.SelectEndOfBlock]
        else:
            act = [QWebPage.WebAction.MoveToPreviousLine, QWebPage.WebAction.MoveToEndOfBlock]
        for _ in range(count):
            for a in act:
                self._widget.triggerPageAction(a)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line_to_start()

    def move_to_start_of_document(self):
        if self._selection_state is not browsertab.SelectionState.none:
            act = QWebPage.WebAction.SelectStartOfDocument
        else:
            act = QWebPage.WebAction.MoveToStartOfDocument
        self._widget.triggerPageAction(act)
        if self._selection_state is browsertab.SelectionState.line:
            self._select_line()

    def move_to_end_of_document(self):
        if self._selection_state is not browsertab.SelectionState.none:
            act = QWebPage.WebAction.SelectEndOfDocument
        else:
            act = QWebPage.WebAction.MoveToEndOfDocument
        self._widget.triggerPageAction(act)

    def toggle_selection(self, line=False):
        if line:
            self._selection_state = browsertab.SelectionState.line
            self._select_line()
            self.reverse_selection()
            self._select_line()
            self.reverse_selection()
        elif self._selection_state is not browsertab.SelectionState.normal:
            self._selection_state = browsertab.SelectionState.normal
        else:
            self._selection_state = browsertab.SelectionState.none
        self.selection_toggled.emit(self._selection_state)

    def drop_selection(self):
        self._widget.triggerPageAction(QWebPage.WebAction.MoveToNextChar)

    def selection(self, callback):
        callback(self._widget.selectedText())

    def reverse_selection(self):
        self._tab.run_js_async("""{
            const sel = window.getSelection();
            sel.setBaseAndExtent(
                sel.extentNode, sel.extentOffset, sel.baseNode,
                sel.baseOffset
            );
        }""")

    def _select_line(self):
        self._widget.triggerPageAction(QWebPage.WebAction.SelectStartOfLine)
        self.reverse_selection()
        self._widget.triggerPageAction(QWebPage.WebAction.SelectEndOfLine)
        self.reverse_selection()

    def _select_line_to_end(self):
        # direction of selection (if anchor is to the left or right
        # of focus) has to be checked before moving selection
        # to the end of line
        if self._js_selection_left_to_right():
            self._widget.triggerPageAction(QWebPage.WebAction.SelectEndOfLine)

    def _select_line_to_start(self):
        if not self._js_selection_left_to_right():
            self._widget.triggerPageAction(QWebPage.WebAction.SelectStartOfLine)

    def _js_selection_left_to_right(self):
        """Return True iff the selection's direction is left to right."""
        return self._tab.private_api.run_js_sync("""
            var sel = window.getSelection();
            var position = sel.anchorNode.compareDocumentPosition(sel.focusNode);
            (!position && sel.anchorOffset < sel.focusOffset ||
                position === Node.DOCUMENT_POSITION_FOLLOWING);
        """)

    def _follow_selected(self, *, tab=False):
        if QWebSettings.globalSettings().testAttribute(
                QWebSettings.WebAttribute.JavascriptEnabled):
            if tab:
                self._tab.data.override_target = usertypes.ClickTarget.tab
            self._tab.run_js_async("""
                const aElm = document.activeElement;
                if (window.getSelection().anchorNode) {
                    window.getSelection().anchorNode.parentNode.click();
                } else if (aElm && aElm !== document.body) {
                    aElm.click();
                }
            """)
        else:
            selection = self._widget.selectedHtml()
            if not selection:
                # Getting here may mean we crashed, but we can't do anything
                # about that until this commit is released:
                # https://github.com/annulen/webkit/commit/0e75f3272d149bc64899c161f150eb341a2417af
                # TODO find a way to check if something is focused
                self._follow_enter(tab)
                return
            try:
                selected_element = xml.etree.ElementTree.fromstring(
                    '<html>{}</html>'.format(selection)).find('a')
            except xml.etree.ElementTree.ParseError:
                raise browsertab.WebTabError('Could not parse selected '
                                             'element!')

            if selected_element is not None:
                try:
                    href = selected_element.attrib['href']
                except KeyError:
                    raise browsertab.WebTabError('Anchor element without '
                                                 'href!')
                url = self._tab.url().resolved(QUrl(href))
                if tab:
                    self._tab.new_tab_requested.emit(url)
                else:
                    self._tab.load_url(url)

    def follow_selected(self, *, tab=False):
        try:
            self._follow_selected(tab=tab)
        finally:
            self.follow_selected_done.emit()


class WebKitZoom(browsertab.AbstractZoom):

    """QtWebKit implementations related to zooming."""

    _widget: webview.WebView

    def _set_factor_internal(self, factor):
        self._widget.setZoomFactor(factor)


class WebKitScroller(browsertab.AbstractScroller):

    """QtWebKit implementations related to scrolling."""

    # FIXME:qtwebengine When to use the main frame, when the current one?

    _widget: webview.WebView

    def pos_px(self):
        return self._widget.page().mainFrame().scrollPosition()

    def pos_perc(self):
        return self._widget.scroll_pos

    def to_point(self, point):
        self._widget.page().mainFrame().setScrollPosition(point)

    def to_anchor(self, name):
        self._widget.page().mainFrame().scrollToAnchor(name)

    def delta(self, x: int = 0, y: int = 0) -> None:
        qtutils.check_overflow(x, 'int')
        qtutils.check_overflow(y, 'int')
        self._widget.page().mainFrame().scroll(x, y)

    def delta_page(self, x: float = 0.0, y: float = 0.0) -> None:
        if y.is_integer():
            y = int(y)
            if y == 0:
                pass
            elif y < 0:
                self.page_up(count=-y)
            elif y > 0:
                self.page_down(count=y)
            y = 0
        if x == 0 and y == 0:
            return
        size = self._widget.page().mainFrame().geometry()
        self.delta(int(x * size.width()), int(y * size.height()))

    def to_perc(self, x=None, y=None):
        if x is None and y == 0:
            self.top()
        elif x is None and y == 100:
            self.bottom()
        else:
            for val, orientation in [(x, Qt.Orientation.Horizontal), (y, Qt.Orientation.Vertical)]:
                if val is not None:
                    frame = self._widget.page().mainFrame()
                    maximum = frame.scrollBarMaximum(orientation)
                    if maximum == 0:
                        continue
                    pos = int(maximum * val / 100)
                    pos = qtutils.check_overflow(pos, 'int', fatal=False)
                    frame.setScrollBarValue(orientation, pos)

    def _key_press(self, key, count=1, getter_name=None, direction=None):
        frame = self._widget.page().mainFrame()
        getter = None if getter_name is None else getattr(frame, getter_name)

        # FIXME:qtwebengine needed?
        # self._widget.setFocus()

        for _ in range(min(count, 5000)):
            # Abort scrolling if the minimum/maximum was reached.
            if (getter is not None and
                    frame.scrollBarValue(direction) == getter(direction)):
                return
            self._tab.fake_key_press(key)

    def up(self, count=1):
        self._key_press(Qt.Key.Key_Up, count, 'scrollBarMinimum', Qt.Orientation.Vertical)

    def down(self, count=1):
        self._key_press(Qt.Key.Key_Down, count, 'scrollBarMaximum', Qt.Orientation.Vertical)

    def left(self, count=1):
        self._key_press(Qt.Key.Key_Left, count, 'scrollBarMinimum', Qt.Orientation.Horizontal)

    def right(self, count=1):
        self._key_press(Qt.Key.Key_Right, count, 'scrollBarMaximum', Qt.Orientation.Horizontal)

    def top(self):
        self._key_press(Qt.Key.Key_Home)

    def bottom(self):
        self._key_press(Qt.Key.Key_End)

    def page_up(self, count=1):
        self._key_press(Qt.Key.Key_PageUp, count, 'scrollBarMinimum', Qt.Orientation.Vertical)

    def page_down(self, count=1):
        self._key_press(Qt.Key.Key_PageDown, count, 'scrollBarMaximum',
                        Qt.Orientation.Vertical)

    def at_top(self):
        return self.pos_px().y() == 0

    def at_bottom(self):
        frame = self._widget.page().currentFrame()
        return self.pos_px().y() >= frame.scrollBarMaximum(Qt.Orientation.Vertical)


class WebKitHistoryPrivate(browsertab.AbstractHistoryPrivate):

    """History-related methods which are not part of the extension API."""

    _history: QWebHistory

    def __init__(self, tab: 'WebKitTab') -> None:
        self._tab = tab
        self._history = cast(QWebHistory, None)

    def serialize(self):
        return qtutils.serialize(self._history)

    def deserialize(self, data):
        qtutils.deserialize(data, self._history)

    def load_items(self, items):
        if items:
            self._tab.before_load_started.emit(items[-1].url)

        stream, _data, user_data = tabhistory.serialize(items)
        qtutils.deserialize_stream(stream, self._history)
        for i, data in enumerate(user_data):
            self._history.itemAt(i).setUserData(data)

        cur_data = self._history.currentItem().userData()
        if cur_data is not None:
            if 'zoom' in cur_data:
                self._tab.zoom.set_factor(cur_data['zoom'])
            if ('scroll-pos' in cur_data and
                    self._tab.scroller.pos_px() == QPoint(0, 0)):
                QTimer.singleShot(0, functools.partial(
                    self._tab.scroller.to_point, cur_data['scroll-pos']))


class WebKitHistory(browsertab.AbstractHistory):

    """QtWebKit implementations related to page history."""

    def __init__(self, tab):
        super().__init__(tab)
        self.private_api = WebKitHistoryPrivate(tab)

    def __len__(self):
        return len(self._history)

    def __iter__(self):
        return iter(self._history.items())

    def current_idx(self):
        return self._history.currentItemIndex()

    def current_item(self):
        return self._history.currentItem()

    def can_go_back(self):
        return self._history.canGoBack()

    def can_go_forward(self):
        return self._history.canGoForward()

    def _item_at(self, i):
        return self._history.itemAt(i)

    def _go_to_item(self, item):
        self._tab.before_load_started.emit(item.url())
        self._history.goToItem(item)

    def back_items(self):
        return self._history.backItems(self._history.count())

    def forward_items(self):
        return self._history.forwardItems(self._history.count())


class WebKitElements(browsertab.AbstractElements):

    """QtWebKit implementations related to elements on the page."""

    _tab: 'WebKitTab'
    _widget: webview.WebView

    def find_css(self, selector, callback, error_cb, *, only_visible=False):
        utils.unused(error_cb)
        mainframe = self._widget.page().mainFrame()
        if mainframe is None:
            raise browsertab.WebTabError("No frame focused!")

        elems = []
        frames = webkitelem.get_child_frames(mainframe)
        for f in frames:
            frame_elems = cast(Iterable[QWebElement], f.findAllElements(selector))
            for elem in frame_elems:
                elems.append(webkitelem.WebKitElement(elem, tab=self._tab))

        if only_visible:
            # pylint: disable=protected-access
            elems = [e for e in elems if e._is_visible(mainframe)]
            # pylint: enable=protected-access

        callback(elems)

    def find_id(self, elem_id, callback):
        def find_id_cb(elems):
            """Call the real callback with the found elements."""
            if not elems:
                callback(None)
            else:
                callback(elems[0])

        # Escape non-alphanumeric characters in the selector
        # https://www.w3.org/TR/CSS2/syndata.html#value-def-identifier
        elem_id = re.sub(r'[^a-zA-Z0-9_-]', r'\\\g<0>', elem_id)
        self.find_css('#' + elem_id, find_id_cb, error_cb=lambda exc: None)

    def find_focused(self, callback):
        frame = cast(Optional[QWebFrame], self._widget.page().currentFrame())
        if frame is None:
            callback(None)
            return

        elem = frame.findFirstElement('*:focus')
        if elem.isNull():
            callback(None)
        else:
            callback(webkitelem.WebKitElement(elem, tab=self._tab))

    def find_at_pos(self, pos, callback):
        assert pos.x() >= 0
        assert pos.y() >= 0
        frame = cast(Optional[QWebFrame], self._widget.page().frameAt(pos))
        if frame is None:
            # This happens when we click inside the webview, but not actually
            # on the QWebPage - for example when clicking the scrollbar
            # sometimes.
            log.webview.debug("Hit test at {} but frame is None!".format(pos))
            callback(None)
            return

        # You'd think we have to subtract frame.geometry().topLeft() from the
        # position, but it seems QWebFrame::hitTestContent wants a position
        # relative to the QWebView, not to the frame. This makes no sense to
        # me, but it works this way.
        hitresult = frame.hitTestContent(pos)
        if hitresult.isNull():
            # For some reason, the whole hit result can be null sometimes (e.g.
            # on doodle menu links).
            log.webview.debug("Hit test result is null!")
            callback(None)
            return

        try:
            elem = webkitelem.WebKitElement(hitresult.element(), tab=self._tab)
        except webkitelem.IsNullError:
            # For some reason, the hit result element can be a null element
            # sometimes (e.g. when clicking the timetable fields on
            # https://www.sbb.ch/ ).
            log.webview.debug("Hit test result element is null!")
            callback(None)
            return

        callback(elem)


class WebKitAudio(browsertab.AbstractAudio):

    """Dummy handling of audio status for QtWebKit."""

    def set_muted(self, muted: bool, override: bool = False) -> None:
        raise browsertab.WebTabError('Muting is not supported on QtWebKit!')

    def is_muted(self):
        return False

    def is_recently_audible(self):
        return False


class WebKitTabPrivate(browsertab.AbstractTabPrivate):

    """QtWebKit-related methods which aren't part of the public API."""

    _widget: webview.WebView

    def networkaccessmanager(self):
        return self._widget.page().networkAccessManager()

    def clear_ssl_errors(self):
        self.networkaccessmanager().clear_all_ssl_errors()

    def event_target(self):
        return self._widget

    def shutdown(self):
        self._widget.shutdown()

    def run_js_sync(self, code):
        document_element = self._widget.page().mainFrame().documentElement()
        result = document_element.evaluateJavaScript(code)
        return result

    def _init_inspector(self, splitter, win_id, parent=None):
        return webkitinspector.WebKitInspector(splitter, win_id, parent)


class WebKitTab(browsertab.AbstractTab):

    """A QtWebKit tab in the browser."""

    _widget: webview.WebView

    def __init__(self, *, win_id, mode_manager, private, parent=None):
        super().__init__(win_id=win_id,
                         mode_manager=mode_manager,
                         private=private,
                         parent=parent)
        widget = webview.WebView(win_id=win_id, tab_id=self.tab_id,
                                 private=private, tab=self)
        if private:
            self._make_private(widget)
        self.history = WebKitHistory(tab=self)
        self.scroller = WebKitScroller(tab=self, parent=self)
        self.caret = WebKitCaret(mode_manager=mode_manager,
                                 tab=self, parent=self)
        self.zoom = WebKitZoom(tab=self, parent=self)
        self.search = WebKitSearch(tab=self, parent=self)
        self.printing = WebKitPrinting(tab=self, parent=self)
        self.elements = WebKitElements(tab=self)
        self.action = WebKitAction(tab=self)
        self.audio = WebKitAudio(tab=self, parent=self)
        self.private_api = WebKitTabPrivate(mode_manager=mode_manager,
                                            tab=self)
        # We're assigning settings in _set_widget
        self.settings = webkitsettings.WebKitSettings(settings=None)
        self._set_widget(widget)
        self._connect_signals()
        self.backend = usertypes.Backend.QtWebKit

    def _install_event_filter(self):
        self._widget.installEventFilter(self._tab_event_filter)

    def _make_private(self, widget):
        settings = widget.settings()
        settings.setAttribute(QWebSettings.WebAttribute.PrivateBrowsingEnabled, True)

    def load_url(self, url):
        self._load_url_prepare(url)
        self._widget.load(url)

    def url(self, *, requested=False):
        frame = self._widget.page().mainFrame()
        if requested:
            return frame.requestedUrl()
        else:
            return frame.url()

    def dump_async(self, callback, *, plain=False):
        frame = self._widget.page().mainFrame()
        if plain:
            callback(frame.toPlainText())
        else:
            callback(frame.toHtml())

    def run_js_async(self, code, callback=None, *, world=None):
        if world is not None and world != usertypes.JsWorld.jseval:
            log.webview.warning("Ignoring world ID {}".format(world))
        result = self.private_api.run_js_sync(code)
        if callback is not None:
            callback(result)

    def icon(self):
        return self._widget.icon()

    def reload(self, *, force=False):
        if force:
            action = QWebPage.WebAction.ReloadAndBypassCache
        else:
            action = QWebPage.WebAction.Reload
        self._widget.triggerPageAction(action)

    def stop(self):
        self._widget.stop()

    def title(self):
        return self._widget.title()

    def renderer_process_pid(self) -> Optional[int]:
        return None

    @pyqtSlot()
    def _on_history_trigger(self):
        url = self.url()
        requested_url = self.url(requested=True)
        self.history_item_triggered.emit(url, requested_url, self.title())

    def set_html(self, html, base_url=QUrl()):
        self._widget.setHtml(html, base_url)

    @pyqtSlot()
    def _on_load_started(self):
        super()._on_load_started()
        nam = self._widget.page().networkAccessManager()
        assert isinstance(nam, networkmanager.NetworkManager), nam
        nam.netrc_used = False
        # Make sure the icon is cleared when navigating to a page without one.
        self.icon_changed.emit(QIcon())

    @pyqtSlot(bool)
    def _on_load_finished(self, ok: bool) -> None:
        super()._on_load_finished(ok)
        self._update_load_status(ok)

    @pyqtSlot()
    def _on_frame_load_finished(self):
        """Make sure we emit an appropriate status when loading finished.

        While Qt has a bool "ok" attribute for loadFinished, it always is True
        when using error pages... See
        https://github.com/qutebrowser/qutebrowser/issues/84
        """
        page = self._widget.page()
        assert isinstance(page, webpage.BrowserPage), page
        self._on_load_finished(not page.error_occurred)

    @pyqtSlot()
    def _on_webkit_icon_changed(self):
        """Emit iconChanged with a QIcon like QWebEngineView does."""
        if sip.isdeleted(self._widget):
            log.webview.debug("Got _on_webkit_icon_changed for deleted view!")
            return
        self.icon_changed.emit(self._widget.icon())

    @pyqtSlot(QWebFrame)
    def _on_frame_created(self, frame):
        """Connect the contentsSizeChanged signal of each frame."""
        # FIXME:qtwebengine those could theoretically regress:
        # https://github.com/qutebrowser/qutebrowser/issues/152
        # https://github.com/qutebrowser/qutebrowser/issues/263
        frame.contentsSizeChanged.connect(self._on_contents_size_changed)

    @pyqtSlot(QSize)
    def _on_contents_size_changed(self, size):
        self.contents_size_changed.emit(QSizeF(size))

    @pyqtSlot(usertypes.NavigationRequest)
    def _on_navigation_request(self, navigation):
        super()._on_navigation_request(navigation)
        if not navigation.accepted:
            return

        log.webview.debug("target {} override {}".format(
            self.data.open_target, self.data.override_target))

        if self.data.override_target is not None:
            target = self.data.override_target
            self.data.override_target = None
        else:
            target = self.data.open_target

        if (navigation.navigation_type == navigation.Type.link_clicked and
                target != usertypes.ClickTarget.normal):
            tab = shared.get_tab(self.win_id, target)
            tab.load_url(navigation.url)
            self.data.open_target = usertypes.ClickTarget.normal
            navigation.accepted = False

        if navigation.is_main_frame:
            self.settings.update_for_url(navigation.url)

    @pyqtSlot('QNetworkReply*')
    def _on_ssl_errors(self, reply):
        self._insecure_hosts.add(reply.url().host())

    def _connect_signals(self):
        view = self._widget
        page = view.page()
        frame = page.mainFrame()
        page.windowCloseRequested.connect(  # type: ignore[attr-defined]
            self.window_close_requested)
        page.linkHovered.connect(  # type: ignore[attr-defined]
            self.link_hovered)
        page.loadProgress.connect(  # type: ignore[attr-defined]
            self._on_load_progress)
        frame.loadStarted.connect(  # type: ignore[attr-defined]
            self._on_load_started)
        view.scroll_pos_changed.connect(self.scroller.perc_changed)
        view.titleChanged.connect(  # type: ignore[attr-defined]
            self.title_changed)
        view.urlChanged.connect(  # type: ignore[attr-defined]
            self._on_url_changed)
        view.shutting_down.connect(self.shutting_down)
        page.networkAccessManager().sslErrors.connect(self._on_ssl_errors)
        frame.loadFinished.connect(  # type: ignore[attr-defined]
            self._on_frame_load_finished)
        view.iconChanged.connect(  # type: ignore[attr-defined]
            self._on_webkit_icon_changed)
        page.frameCreated.connect(  # type: ignore[attr-defined]
            self._on_frame_created)
        frame.contentsSizeChanged.connect(  # type: ignore[attr-defined]
            self._on_contents_size_changed)
        frame.initialLayoutCompleted.connect(  # type: ignore[attr-defined]
            self._on_history_trigger)
        page.navigation_request.connect(  # type: ignore[attr-defined]
            self._on_navigation_request)
