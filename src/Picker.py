# main.py
#
# Copyright 2021 Lorenzo Paderi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import gi
import os
import threading
import subprocess
from time import time, time_ns, sleep
from typing import Optional
import re

from .ShortcutsWindow import ShortcutsWindow
from .components.CustomTagEntry import CustomTagEntry
from .components.SkintoneSelector import SkintoneSelector
from .components.FlowBoxChild import FlowBoxChild
from .components.EmojiButton import EmojiButton
from .lib.custom_tags import get_custom_tags
from .lib.localized_tags import get_localized_tags
from .lib.emoji_history import increment_emoji_usage_counter, get_history
from .utils import tag_list_contains, debounce, idle
from .lib.DbusService import DbusService, DBUS_SERVICE_INTERFACE, DBUS_SERVICE_PATH
from .assets.emoji_list import emojis, emoji_categories

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, Gdk, Adw, GLib, Pango  # noqa


class Picker(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(title="Smile", resizable=True, *args, **kwargs)

        EMOJI_LIST_MIN_HEIGHT = 320

        self.set_default_size(1, 1)

        self.last_copied_text = None

        self.event_controller_keys = Gtk.EventControllerKey()
        self.event_controller_keys.connect('key-pressed', self.handle_window_key_press)
        self.event_controller_keys.connect('key-released', self.handle_window_key_release)
        self.add_controller(self.event_controller_keys)
        self.data_dir = Gio.Application.get_default().datadir

        self.settings: Gio.Settings = Gio.Settings.new('it.mijorus.smile')
        self.settings.connect('changed::skintone-modifier', self.update_emoji_skintones)

        self.EMOJI_GRID_COL_N = 5
        self.emoji_grid_first_row = []

        self.selected_category_index = 0
        self.selected_category = 'smileys-emotion'
        self.query: str = None
        self.selection: list[str] = []
        self.selected_buttons: list[EmojiButton] = []
        
        self.history = []
        # self.history_size = 0

        self.clipboard = Gdk.Display.get_default().get_clipboard()

        # Create the emoji list and category picker
        self.categories_count = 0
        self.viewport_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=['viewport'], vexpand=True)

        self.list_tip_revealer = Gtk.Revealer(
            reveal_child=False, 
            transition_type=Gtk.RevealerTransitionType.NONE, 
            css_classes=['solid-overlay'],
            visible=False
            # valign=Gtk.Align.START,
        )

        self.list_tip_label = Gtk.Label(
            label= _("Whoa, it's still empty! \nYour most used emojis will show up here\n"),
            css_classes=['dim-label'], 
            justify=Gtk.Justification.CENTER
        )

        self.list_tip_revealer.set_child(self.list_tip_label)

        self.select_buffer_label = Gtk.Label(margin_bottom=2, css_classes=['title-2'], hexpand=True, halign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.START)
        select_buffer_button = Gtk.Button(icon_name='arrow2-right-symbolic', valign=Gtk.Align.CENTER)
        pop_buffer_btn = Gtk.Button(icon_name='smile-entry-clear-symbolic', valign=Gtk.Align.CENTER, css_classes=['flat'])

        select_buffer_button.connect('clicked', lambda w: self.copy_and_quit())
        pop_buffer_btn.connect('clicked', lambda w: self.deselect_emoji_button())
        select_buffer_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=['selected-emojis-box'], spacing=2)
        [select_buffer_container.append(w) for w in [self.select_buffer_label, pop_buffer_btn, select_buffer_button]]

        self.select_buffer_revealer = Gtk.Revealer(
            reveal_child=False, 
            css_classes=['solid-overlay'],
            child=select_buffer_container, 
            valign=Gtk.Align.END
        )

        self.emoji_list_widgets: list[FlowBoxChild] = []
        self.emoji_list = Gtk.FlowBox(
            valign=Gtk.Align.START,
            homogeneous=True,
            css_classes=['emoji_list_box'],
            margin_top=2,
            margin_bottom=2,
            selection_mode=Gtk.SelectionMode.SINGLE,
            max_children_per_line=self.EMOJI_GRID_COL_N,
            min_children_per_line=self.EMOJI_GRID_COL_N
        )

        self.refresh_emoji_list()
        self.category_picker_widgets: list[Gtk.Button] = []
        self.category_picker = self.create_category_picker()

        scrolled_emoji_window = Gtk.ScrolledWindow(
            min_content_height=EMOJI_LIST_MIN_HEIGHT, 
            propagate_natural_height=True, 
            propagate_natural_width=True, 
            vexpand=True
        )

        scrolled_emoji_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_container = Adw.Clamp(maximum_size=600)


        scrolled_container.set_child(self.emoji_list)
        scrolled_emoji_window.set_child(scrolled_container)

        emoji_list_overlay_container = Gtk.Overlay(child=scrolled_emoji_window)

        emoji_list_overlay_container.add_overlay(self.list_tip_revealer)
        emoji_list_overlay_container.add_overlay(self.select_buffer_revealer)

        # self.viewport_box.append(self.list_tip_revealer)
        # self.viewport_box.append(self.select_buffer_revealer)
        self.viewport_box.append(emoji_list_overlay_container)
        self.viewport_box.append(self.category_picker)

        # Create search entry
        search_container = Gtk.Box()

        self.search_entry = Gtk.SearchEntry(hexpand=True, width_request=200)
        self.search_entry.connect('search_changed', self.search_emoji)
        self.search_entry.connect('activate', self.handle_search_entry_activate)
        search_container.append(self.search_entry)

        search_controller_key = Gtk.EventControllerKey()
        search_controller_key.connect(
            'key-pressed',
            lambda q, w, e, r: self.default_hiding_action(paste_on_exit=False) if w == Gdk.KEY_Escape else False
        )

        self.search_entry.add_controller(search_controller_key)

        # Create an header bar
        header_bar = Adw.HeaderBar(title_widget=Gtk.Box(), decoration_layout='icon:close', css_classes=['flat'])
        menu_button = self.create_menu_button()
        header_bar.pack_start(menu_button)
        header_bar.set_title_widget(search_container)

        self.set_titlebar(header_bar)

        self.shortcut_window: ShortcutsWindow = None
        self.shift_key_pressed = False

        # Display custom tags at the top of the list when searching
        # This variable the status of the sorted status
        self.skintone_selector: Optional[SkintoneSelector] = None

        self.overlay = Adw.ToastOverlay()
        self.overlay.set_child(self.viewport_box)

        self.update_emoji_skintones(self.settings, 'skintone-modifier')
        self.set_active_category('smileys-emotion')

        self.set_child(self.overlay)
        self.search_entry.grab_focus()

    def on_activation(self):
        self.present_with_time(Gdk.CURRENT_TIME)
        self.grab_focus()

        self.emoji_list.unselect_all()

        if self.settings.get_boolean('iconify-on-esc'):
            self.unminimize()

        self.set_focus(self.search_entry)

    # Create stuff
    def create_menu_button(self):
        builder = Gtk.Builder()
        builder.add_from_resource('/it/mijorus/smile/ui/menu.ui')
        menu = builder.get_object('primary_menu')

        return Gtk.MenuButton(menu_model=menu, icon_name='open-menu-symbolic')

    def create_category_picker(self) -> Gtk.Box:
        box = Gtk.Box(spacing=0, halign=Gtk.Align.CENTER, hexpand=True, name='emoji_categories_box')

        i = 0
        for c, cat in emoji_categories.items():
            if not 'icon' in cat:
                continue

            button = Gtk.Button(icon_name=cat['icon'], valign=Gtk.Align.CENTER)
            button.category = c
            button.index = i
            button.connect('clicked', self.filter_for_category)

            box.append(button)
            self.category_picker_widgets.append(button)
            i += 1

        self.categories_count = i

        return box

    def refresh_emoji_list(self):
        start = time_ns()

        self.emoji_list.remove_all()
        self.emoji_list_widgets = []

        self.history = get_history()
        filter_for_recents = self.selected_category == 'recents'
        tags_locale = self.settings.get_string('tags-locale')
        merge_english_tags = self.settings.get_boolean('merge-english-tags')
        tags_locale_is_en = tags_locale == 'en'

        use_localised_tags = self.settings.get_boolean('use-localized-tags') if self.query else False

        for key, emoji in emojis.items():
            is_recent = (emoji['hexcode'] in self.history)

            if self.query:
                localized_tags = []
                filter_result = True

                if use_localised_tags and not tags_locale_is_en:
                    localized_tags = get_localized_tags(tags_locale, emoji['hexcode'], self.data_dir)

                custom_tags = ''
                if self.query != emoji['emoji']:
                    custom_tags = get_custom_tags(emoji['hexcode'], cache=True)

                if self.query == emoji['emoji']:
                    filter_result = True
                elif custom_tags and tag_list_contains(custom_tags, self.query):
                    filter_result = True
                elif use_localised_tags:
                    if merge_english_tags:
                        filter_result = tag_list_contains(','.join(localized_tags), self.query) or tag_list_contains(emoji['tags'], self.query)
                    else:
                        filter_result = tag_list_contains(','.join(localized_tags), self.query)
                elif not use_localised_tags:
                    filter_result = tag_list_contains(emoji['tags'], self.query)
                else:
                    filter_result = False
                
                if not filter_result: 
                    continue
            else:
                if filter_for_recents:
                    if not is_recent:
                        continue
                elif emoji['group'] != self.selected_category:
                    continue

            emoji_button = EmojiButton(emoji)
            emoji_button.connect('clicked', self.handle_emoji_button_click)

            flowbox_child = FlowBoxChild(emoji_button)

            gesture = Gtk.GestureSingle(button=Gdk.BUTTON_SECONDARY)
            gesture.connect('end', lambda e, _: self.show_skintone_selector(e.get_widget()))
            flowbox_child.add_controller(gesture)

            gesture_mid_click = Gtk.GestureSingle(button=Gdk.BUTTON_MIDDLE)
            gesture_mid_click.connect('end', lambda e, _: self.show_custom_tag_entry(e.get_widget()))
            flowbox_child.add_controller(gesture_mid_click)

            self.emoji_list.append(flowbox_child)
            self.emoji_list_widgets.append(flowbox_child)

        self.emoji_list.set_sort_func(self.sort_emoji_list, None)
        # print('Emoji list creation took ' + str((time_ns() - start) / 1000000) + 'ms')

    # Handle events
    def handle_emoji_button_click(self, widget: Gtk.Button):
        widget.get_parent().grab_focus()

        if self.settings.get_boolean('mouse-multi-select'):
            if self.shift_key_pressed:
                self.copy_and_quit(widget)
            else:
                self.select_emoji_button(widget)
        else:
            if not self.shift_key_pressed:
                self.copy_and_quit(widget)
            else:
                self.select_emoji_button(widget)

    # Handle key-presses
    def handle_window_key_release(self, controller: Gtk.EventController, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        if (keyval == Gdk.KEY_Shift_L or keyval == Gdk.KEY_Shift_R):
            self.shift_key_pressed = False

    # Handle every possible keypress here, returns True if the event was handled (prevent default)
    def handle_window_key_press(self, controller: Gtk.EventController, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        if (keyval == Gdk.KEY_Escape):
            self.default_hiding_action(paste_on_exit=False)
            return True

        self.shift_key_pressed = (keyval == Gdk.KEY_Shift_L or keyval == Gdk.KEY_Shift_R)

        ctrl_key = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_key = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt_key = bool(state & Gdk.ModifierType.ALT_MASK)

        is_modifier = ctrl_key or shift_key or alt_key
        keyval_name = Gdk.keyval_name(keyval)

        focused_widget = self.get_focus()
        focused_button = None

        if isinstance(focused_widget, FlowBoxChild):
            focused_button = focused_widget.emoji_button

        if self.search_entry is focused_widget.get_parent():
            if (keyval == Gdk.KEY_Down):
                self.load_first_row()
                if self.emoji_grid_first_row:
                    self.emoji_grid_first_row[0].grab_focus()
                    self.emoji_list.emit('move-cursor', Gtk.MovementStep.BUFFER_ENDS, -1, False, False)

                return True

        if alt_key:
            if focused_button and keyval == Gdk.KEY_e:
                self.show_skintone_selector(focused_widget)
                return True

            elif focused_button and keyval == Gdk.KEY_t:
                self.show_custom_tag_entry(focused_widget)
                return True

            elif keyval in [Gdk.KEY_Left, Gdk.KEY_Right]:
                next_sel = None
                if keyval == Gdk.KEY_Left:
                    next_sel = self.selected_category_index - 1 if (self.selected_category_index > 0) else 0
                elif keyval == Gdk.KEY_Right:
                    next_sel_index = (self.categories_count - 1)
                    next_sel = self.selected_category_index + 1 if (self.selected_category_index < (next_sel_index)) else (next_sel_index)

                if next_sel != None:
                    for child in list(self.category_picker_widgets):
                        if child.index == next_sel:
                            self.filter_for_category(child)
                            return True

            return False

        if shift_key:
            if (keyval == Gdk.KEY_Return):
                if focused_button:
                    self.select_emoji_button(focused_button)
                    return True

            if (keyval == Gdk.KEY_BackSpace):
                if focused_button:
                    self.deselect_emoji_button()

                    return True

        elif ctrl_key:
            if keyval == Gdk.KEY_question:
                shortcut_window = ShortcutsWindow()
                shortcut_window.open()

            elif (keyval == Gdk.KEY_Return):
                if self.selection:
                    self.copy_and_quit()
                    return True

            elif keyval == Gdk.KEY_BackSpace:
                self.query = None
                self.search_entry.set_text('')
                self.search_entry.grab_focus()
                return True

        else:
            # handle key combinations without modifiers
            if (not is_modifier) and (keyval == Gdk.KEY_BackSpace):
                self.search_entry.grab_focus()
                return True

            if focused_button:
                # Focus is on an emoji button
                if (keyval == Gdk.KEY_Return):
                    self.copy_and_quit(focused_button)
                    return True
                elif (not is_modifier) and (len(keyval_name) == 1) and re.match(r'\S', keyval_name):
                    self.search_entry.insert_text(keyval_name, -1)
                    self.search_entry.set_position(-1)
                    self.search_entry.grab_focus()
                    return True
                elif (keyval == Gdk.KEY_Up) and (focused_widget in self.emoji_grid_first_row):
                    self.search_entry.grab_focus()

            elif isinstance(focused_widget, Gtk.Button) and hasattr(focused_widget, 'category'):
                # Focus is on a category button
                # Triggers when we press arrow up on the category picker
                az_re = re.compile(r"[a-z]", re.IGNORECASE)
                if re.match(az_re, keyval_name):
                    self.search_entry.grab_focus()
                else:
                    if (keyval == Gdk.KEY_Up):
                        self.set_active_category(focused_widget.category)

                        for f in self.emoji_list_widgets:
                            if self.selected_category == 'recents':
                                if f.emoji_button.hexcode in get_history():
                                    f.emoji_button.grab_focus()
                                    break
                            else:
                                if f.emoji_button.emoji_data['group'] == self.selected_category:
                                    f.emoji_button.grab_focus()
                                    break

                    return True

        return False

    def handle_skintone_selector_key_press(self, controller: Gtk.EventController, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        shift_key = bool(state & Gdk.ModifierType.SHIFT_MASK)
        focused_widget: FlowBoxChild = self.skintone_selector.get_focus()

        self.shift_key_pressed = (keyval == Gdk.KEY_Shift_L) or (keyval == Gdk.KEY_Shift_R)

        if shift_key:
            self.shift_key_pressed = True
            if (keyval == Gdk.KEY_Return):
                self.select_emoji_button(focused_widget.emoji_button)
                return True

            elif (keyval == Gdk.KEY_BackSpace):
                self.deselect_emoji_button()

                return True
        else:
            if (keyval == Gdk.KEY_Return):
                self.skintone_selector.request_close()
                self.copy_and_quit(focused_widget.emoji_button)
                return True

        return False

    def handle_search_entry_activate(self, entry: Gtk.Entry):
        if self.query:
            self.load_first_row()
            if self.emoji_grid_first_row:
                self.copy_and_quit(self.emoji_grid_first_row[0].emoji_button)

    def send_paste_signal(self):
        if not self.settings.get_boolean('auto-paste') or not self.last_copied_text:
            return

        if DbusService.dbus_connection:
            DbusService.dbus_connection.emit_signal(None, DBUS_SERVICE_PATH, DBUS_SERVICE_INTERFACE, 'CopiedEmoji', GLib.Variant('(s)', (self.last_copied_text,)))
        elif os.getenv('XDG_SESSION_TYPE') != 'wayland':
            subprocess.check_output(['xdotool', 'key', 'ctrl+v'])

    def default_hiding_action(self, paste_on_exit=True):
        self.search_entry.set_text('')
        self.select_buffer_label.set_text('')
        self.select_buffer_revealer.set_reveal_child(False)
        self.query = None
        self.selection = []
        self.set_empty_recent_tip(None)

        for button in self.selected_buttons:
            button.get_parent().deselect()

        for button in self.emoji_list_widgets:
            button.deselect()

        self.selected_buttons = []

        if self.settings.get_boolean('iconify-on-esc'):
            self.minimize()
            if paste_on_exit: self.send_paste_signal()
        elif not self.settings.get_boolean('load-hidden-on-startup'):
            # async to avoid blocking the main thread
            def close_patch():
                GLib.idle_add(lambda: self.hide())
                sleep(0.5)
                if paste_on_exit: self.send_paste_signal()

                return GLib.idle_add(lambda: self.close())

            threading.Thread(target=close_patch).start()
        else:
            self.set_visible(False)
            if paste_on_exit: self.send_paste_signal()

    # # # # # #
    def show_skintone_selector(self, focused_widget: FlowBoxChild):
        self.emoji_list.select_child(focused_widget)

        if not SkintoneSelector.check_skintone(focused_widget):
            self.overlay.add_toast(
                Adw.Toast(title="No skintones available", timeout=1)
            )
        else:
            self.skintone_selector = SkintoneSelector(
                focused_widget,
                parent=self,
                click_handler=self.handle_emoji_button_click,
                keypress_handler=self.handle_skintone_selector_key_press,
                emoji_active_selection=self.selected_buttons
            )

    def show_custom_tag_entry(self, focused_widget: FlowBoxChild):
        CustomTagEntry(focused_widget, self)

    def set_empty_recent_tip(self, enabled: bool):
        self.list_tip_revealer.set_visible(enabled)
        self.list_tip_revealer.set_reveal_child(enabled)

    def update_selection_content(self, selection: str = None):
        if selection:
            self.select_buffer_label.set_label(''.join(selection))
        else:
            self.select_buffer_label.set_label('')

        self.select_buffer_revealer.set_reveal_child(True if selection else False)

    def set_active_category(self, category: str):
        for b in self.category_picker_widgets:
            if b.category != category:
                b.get_style_context().remove_class('selected')
            else:
                b.get_style_context().add_class('selected')

    def select_emoji_button(self, button: EmojiButton):
        self.selected_buttons.append(button)
        self.selection.append(button.get_label())
        self.emoji_list.select_child(button.get_parent())

        increment_emoji_usage_counter(button)

        button.get_parent().set_as_selected()
        button.get_parent().set_as_active()

        if button.base_skintone_widget:
            button.base_skintone_widget.set_as_selected()

        self.update_selection_content(self.selection)

    def deselect_emoji_button(self):
        if not self.selection:
            return

        last_button = self.selected_buttons[-1]

        self.selection.pop()
        self.selected_buttons.pop()

        if not last_button.get_label() in self.selection:
            last_button.get_parent().deselect()

        if last_button.base_skintone_widget:
            base_skintone_widget_is_selected = False

            for sb in self.selected_buttons:
                if sb.base_skintone_widget is last_button.base_skintone_widget:
                    base_skintone_widget_is_selected = True
                    break

            if not base_skintone_widget_is_selected:
                last_button.base_skintone_widget.deselect()

        self.update_selection_content(self.selection)

    def load_first_row(self):
        self.emoji_grid_first_row = []
        for widget in self.emoji_list_widgets:
            if (len(self.emoji_grid_first_row) < self.EMOJI_GRID_COL_N) and widget.props.visible:
                self.emoji_grid_first_row.append(widget)

    def filter_for_category(self, widget: Gtk.Button):
        self.set_active_category(widget.category)
        widget.grab_focus()

        self.query = None
        self.selected_category = widget.category
        self.selected_category_index = widget.index

        show_empty_recent_tip = widget.category == 'recents' and not get_history()
        self.set_empty_recent_tip(show_empty_recent_tip)

        self.refresh_emoji_list()
        self.load_first_row()

    def copy_and_quit(self, button: Gtk.Button = None):
        text = ''
        if button:
            text = button.get_label()
            increment_emoji_usage_counter(button)

        copied_text = ''.join([*self.selection, text])
        contx = Gdk.ContentProvider.new_for_value(copied_text)
        self.clipboard.set_content(contx)

        self.last_copied_text = copied_text

        if self.settings.get_boolean('is-first-run'):
            n = Gio.Notification.new(_('Copied!'))
            n.set_body(_("I have copied the emoji to the clipboard. You can now paste it in any input field."))
            n.set_icon(Gio.ThemedIcon.new('dialog-information'))

            Gio.Application.get_default().send_notification('copy-message', n)
            self.settings.set_boolean('is-first-run', False)

        self.default_hiding_action()

    @debounce(0.2)
    @idle
    def search_emoji(self, search_entry: str):
        start = time_ns()

        self.search_entry.grab_focus()
        query = search_entry.get_text().strip()

        self.query = query if query else None

        self.refresh_emoji_list()
        self.emoji_list.invalidate_sort()
        # print('Search took ' + str((time_ns() - start) / 1000000) + 'ms')

    def sort_emoji_list(self, child1: Gtk.FlowBoxChild, child2: Gtk.FlowBoxChild, user_data):
        child1 = child1.get_child()
        child2 = child2.get_child()

        if (self.selected_category == 'recents'):
            h1 = self.history[child1.hexcode] if child1.hexcode in self.history else None
            h2 = self.history[child2.hexcode] if child2.hexcode in self.history else None
            return ((h2['lastUsage'] if h2 else 0) - (h1['lastUsage'] if h1 else 0))

        elif self.query:
            return -1 if get_custom_tags(child1.hexcode, True) else 1

        else:
            return (child1.emoji_data['order'] - child2.emoji_data['order'])

    def update_emoji_skintones(self, settings: Gio.Settings, key):
        modifier_settings = self.settings.get_string('skintone-modifier')
        for child in self.emoji_list_widgets:
            emoji_button = child.emoji_button

            if 'skintones' in emoji_button.emoji_data:
                if len(modifier_settings):
                    for tone in emoji_button.emoji_data['skintones']:
                        if f'-{modifier_settings}' in tone['hexcode']:
                            emoji_button.set_label(tone['emoji'])
                            break
                else:
                    emoji_button.set_label(emoji_button.emoji_data['emoji'])
