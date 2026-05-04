import gi
import subprocess
import threading
import re
import os
import time
from datetime import datetime

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Adw, GLib, Gio, Pango

class SmartParser:
    """parses raw smartctl output into structured python dicts"""

    @staticmethod
    def detect_drives() -> list[str]:
        """detect available block devices (sda, sdb, nvme0n1, etc.)"""
        drives = []
        try:
            result = subprocess.run(
                ["lsblk", "-dno", "NAME,TYPE"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] in ("disk",):
                    drives.append(f"/dev/{parts[0]}")
        except Exception:
            for name in sorted(os.listdir("/dev")):
                if re.match(r"^(sd[a-z]|nvme\d+n\d+|hd[a-z]|vd[a-z])$", name):
                    drives.append(f"/dev/{name}")
        return drives if drives else ["/dev/sda"]

    @staticmethod
    def run_smartctl(device: str, timeout: int = 120) -> tuple[str, str, int]:
        """
        run smartctl -x on the given device
        returns (stdout, stderr, returncode)
        tries pkexec if not root

        some drives may become sluggish when failing - use a longer timeout and a retry pass
        so the UI stays responsive and gives a clearer failure message rather than timing out
        immediately on transient disk latency.
        """
        cmd = ["smartctl", "-x", device]
        if os.geteuid() != 0:
            # try via pkexec for privilege escalation
            cmd = ["pkexec"] + cmd

        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
                return result.stdout, result.stderr, result.returncode
            except subprocess.TimeoutExpired:
                if attempt < attempts:
                    # give the drive a short pause, then retry once.
                    time.sleep(1)
                    continue
                return "", "Command timed out (smartctl).", -1
            except FileNotFoundError:
                return "", "smartctl not found. Install smartmontools.", -1
            except Exception as e:
                return "", str(e), -1

    @staticmethod
    def parse(raw: str) -> dict:
        """Parse smartctl -x output into structured sections."""
        data = {
            "drive_info": {},
            "health": {},
            "metrics": {},
            "attributes": [],
            "errors": [],
            "raw": raw,
        }

        lines = raw.splitlines()

        # drive info
        patterns = {
            "model":    r"Device Model:\s+(.+)",
            "model2":   r"Model Number:\s+(.+)",       # NVMe
            "serial":   r"Serial Number:\s+(.+)",
            "firmware": r"Firmware Version:\s+(.+)",
            "capacity": r"User Capacity:\s+(.+)",
            "capacity2":r"Total NVM Capacity:\s+(.+)", # NVMe
            "rpm":      r"Rotation Rate:\s+(.+)",
            "form":     r"Form Factor:\s+(.+)",
            "type":     r"Device is:\s+(.+)",
        }

        for line in lines:
            for key, pattern in patterns.items():
                m = re.search(pattern, line)
                if m:
                    base = key.rstrip("2")
                    if base not in data["drive_info"]:
                        data["drive_info"][base] = m.group(1).strip()

        # health status
        for line in lines:
            if "SMART overall-health self-assessment test result:" in line:
                status = line.split(":")[-1].strip()
                data["health"]["overall"] = status
                data["health"]["passed"] = "PASSED" in status.upper()
            if "Drive failure expected" in line or "FAILED!" in line:
                data["health"]["failure_predicted"] = True
            if "No Errors Logged" in line:
                data["health"]["no_errors"] = True

        if "overall" not in data["health"]:
            data["health"]["overall"] = "UNKNOWN"
            data["health"]["passed"] = None

        # key metrics
        metric_map = {
            "Reallocated_Sector_Ct": "reallocated_sectors",
            "Current_Pending_Sector": "pending_sectors",
            "Power_On_Hours": "power_on_hours",
            "Temperature_Celsius": "temperature",
            "Airflow_Temperature_Cel": "temperature",
            "Power_Cycle_Count": "power_cycles",
            "Reported_Uncorrect": "uncorrectable_errors",
        }

        # NVMe specific
        for line in lines:
            m = re.search(r"Temperature:\s+(\d+)\s+Celsius", line)
            if m and "temperature" not in data["metrics"]:
                data["metrics"]["temperature"] = int(m.group(1))
            m = re.search(r"Power On Hours:\s+([\d,]+)", line)
            if m and "power_on_hours" not in data["metrics"]:
                data["metrics"]["power_on_hours"] = m.group(1).replace(",", "")
            m = re.search(r"Power Cycles:\s+([\d,]+)", line)
            if m and "power_cycles" not in data["metrics"]:
                data["metrics"]["power_cycles"] = m.group(1).replace(",", "")

        # SMARTattributes table 
        in_table = False
        for line in lines:
            if re.match(r"\s*ID#\s+ATTRIBUTE_NAME", line):
                in_table = True
                continue
            if in_table:
                if not line.strip() or line.startswith("==="):
                    in_table = False
                    continue

                parts = line.split()
                if len(parts) < 7 or not parts[0].isdigit():
                    continue

                attr_id = int(parts[0])
                attr_name = parts[1].replace("_", " ")

                # in many vendor formats, raw value is the last column
                # some have 9+ columns, others 7-8. extract robustly
                try:
                    value = int(parts[3])
                except ValueError:
                    continue
                try:
                    worst = int(parts[4])
                except ValueError:
                    worst = 0
                try:
                    thresh = int(parts[5])
                except ValueError:
                    thresh = 0

                raw = " ".join(parts[6:]).strip()
                if not raw:
                    raw = "0"

                raw_num = re.search(r"(\d+)", raw)
                raw_val = int(raw_num.group(1)) if raw_num else 0

                plain = parts[1]
                if plain in metric_map:
                    mkey = metric_map[plain]
                    if mkey not in data["metrics"]:
                        data["metrics"][mkey] = raw_val

                critical = (
                    attr_id in (5, 187, 197, 198) and raw_val > 0
                ) or (thresh > 0 and value <= thresh)

                data["attributes"].append({
                    "id":       attr_id,
                    "name":     attr_name,
                    "value":    value,
                    "worst":    worst,
                    "thresh":   thresh,
                    "raw":      raw,
                    "raw_val":  raw_val,
                    "critical": critical,
                })

        # error log
        in_errors = False
        error_block = []
        for line in lines:
            if "Error Log" in line and "SMART" in line:
                in_errors = True
                continue
            if in_errors:
                if line.startswith("===") and error_block:
                    break
                if line.strip():
                    error_block.append(line)

        # parse individual errors
        current_error = None
        for line in error_block:
            m = re.match(r"Error\s+(\d+)\s+\[", line)
            if m:
                if current_error:
                    data["errors"].append(current_error)
                current_error = {"num": m.group(1), "details": []}
            elif current_error and line.strip():
                current_error["details"].append(line.strip())

        if current_error:
            data["errors"].append(current_error)

        return data

def make_status_badge(text: str, style: str) -> Gtk.Label:
    """create a colored pill-shaped status badge"""
    label = Gtk.Label(label=text)
    label.set_halign(Gtk.Align.START)
    label.add_css_class("badge")
    label.add_css_class(f"badge-{style}")  # badge-success / badge-warning / badge-error
    return label


def make_info_row(title: str, value: str) -> Adw.ActionRow:
    """create a clean Adw.ActionRow showing a key-value pair"""
    row = Adw.ActionRow()
    row.set_title(title)
    row.set_subtitle(value or "—")
    row.set_subtitle_selectable(True)
    return row


def make_section_label(text: str) -> Gtk.Label:
    """bold uppercase section header"""
    lbl = Gtk.Label(label=text)
    lbl.set_halign(Gtk.Align.START)
    lbl.set_margin_top(16)
    lbl.set_margin_bottom(4)
    lbl.add_css_class("heading")
    return lbl

class DriveHealthWindow(Adw.ApplicationWindow):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("SMARTreader")
        self.set_default_size(800, 720)

        self._current_device = None
        self._auto_refresh = False
        self._auto_refresh_source = None

        self._build_ui()
        self._populate_drives()

    # css styling for badges, tables, and conditional formatting
    def _apply_css(self):
        css = b"""
        .badge {
            border-radius: 9999px;
            padding: 2px 10px;
            font-size: 0.8em;
            font-weight: bold;
        }
        .badge-success { background-color: #26a269; color: white; }
        .badge-warning { background-color: #e5a50a; color: white; }
        .badge-error   { background-color: #e01b24; color: white; }
        .badge-neutral { background-color: #5c5c5c; color: white; }

        .attr-row-critical { background-color: alpha(#e01b24, 0.12); }
        .attr-row-warn     { background-color: alpha(#e5a50a, 0.12); }

        .metric-value {
            font-size: 1.5em;
            font-weight: bold;
        }
        .metric-card {
            border-radius: 12px;
            padding: 12px;
            min-width: 130px;
        }
        .temp-hot  { color: #e01b24; }
        .temp-warm { color: #e5a50a; }
        .temp-ok   { color: #26a269; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ui construction
    def _build_ui(self):
        self._apply_css()

        # ToolbarView wraps header + content
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # header with drive selector, title, and action buttons
        header = Adw.HeaderBar()
        header.set_centering_policy(Adw.CenteringPolicy.STRICT)

        # drive selector dropdown
        self._drive_combo = Gtk.DropDown()
        self._drive_combo.set_valign(Gtk.Align.CENTER)
        self._drive_combo.connect("notify::selected-item", self._on_drive_selected)
        header.pack_start(self._drive_combo)

        # title
        title_widget = Adw.WindowTitle()
        title_widget.set_title("SMARTreader")
        title_widget.set_subtitle("SMART Disk Analysis")
        header.set_title_widget(title_widget)

        # refresh button
        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh SMART data")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        header.pack_end(refresh_btn)

        # autorefresh toggle
        self._auto_btn = Gtk.ToggleButton()
        self._auto_btn.set_icon_name("media-playback-start-symbolic")
        self._auto_btn.set_tooltip_text("Auto-refresh every 30s")
        self._auto_btn.connect("toggled", self._on_auto_refresh_toggled)
        header.pack_end(self._auto_btn)

        # run SMART test button
        test_btn = Gtk.Button()
        test_btn.set_icon_name("system-run-symbolic")
        test_btn.set_tooltip_text("Run short SMART self-test")
        test_btn.connect("clicked", self._on_run_test_clicked)
        header.pack_end(test_btn)

        toolbar_view.add_top_bar(header)

        # scrollable main content area 
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(860)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(32)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scroll.set_child(clamp)

        # stack: placeholder vs content
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        clamp.set_child(self._stack)

        # placeholder (loading/empty)
        self._status_page = Adw.StatusPage()
        self._status_page.set_icon_name("drive-harddisk-symbolic")
        self._status_page.set_title("No Drive Selected")
        self._status_page.set_description("Select a drive from the dropdown above to view SMART data.")
        self._stack.add_named(self._status_page, "placeholder")

        # content page
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._stack.add_named(self._content_box, "content")

        self._stack.set_visible_child_name("placeholder")

    # drive selection

    def _populate_drives(self):
        drives = SmartParser.detect_drives()
        model = Gtk.StringList()
        for d in drives:
            model.append(d)
        self._drive_combo.set_model(model)
        self._drive_combo.set_selected(0)

    def _on_drive_selected(self, combo, _param):
        item = combo.get_selected_item()
        if item:
            device = item.get_string()
            if device != self._current_device:
                self._current_device = device
                self._load_smart_data(device)

    # data loading

    def _on_refresh_clicked(self, _btn):
        if self._current_device:
            self._load_smart_data(self._current_device)

    def _on_auto_refresh_toggled(self, btn):
        self._auto_refresh = btn.get_active()
        if self._auto_refresh:
            btn.set_icon_name("media-playback-pause-symbolic")
            self._schedule_auto_refresh()
        else:
            btn.set_icon_name("media-playback-start-symbolic")
            if self._auto_refresh_source:
                GLib.source_remove(self._auto_refresh_source)
                self._auto_refresh_source = None

    def _schedule_auto_refresh(self):
        def do_refresh():
            if self._auto_refresh and self._current_device:
                self._load_smart_data(self._current_device)
                return True  # keep repeating
            return False
        self._auto_refresh_source = GLib.timeout_add_seconds(30, do_refresh)

    def _on_run_test_clicked(self, _btn):
        """run a short SMART self-test on selected device"""
        if not self._current_device:
            self._show_toast("No drive selected.")
            return

        def run():
            cmd = ["smartctl", "-t", "short", self._current_device]
            if os.geteuid() != 0:
                cmd = ["pkexec"] + cmd
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                GLib.idle_add(self._show_toast,
                    "Short test started! It will complete in ~2 minutes." if result.returncode <= 1
                    else f"Test failed: {result.stderr[:80]}"
                )
            except Exception as e:
                GLib.idle_add(self._show_toast, f"Error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _load_smart_data(self, device: str):
        """show spinner, fetch data in background thread, then render on main thread when ready"""
        self._status_page.set_icon_name("content-loading-symbolic")
        self._status_page.set_title("Loading…")
        self._status_page.set_description(f"Fetching SMART data for {device}")
        self._stack.set_visible_child_name("placeholder")

        def fetch():
            stdout, stderr, code = SmartParser.run_smartctl(device)
            GLib.idle_add(self._on_data_ready, stdout, stderr, code, device)

        threading.Thread(target=fetch, daemon=True).start()

    def _on_data_ready(self, stdout: str, stderr: str, code: int, device: str):
        """called on main thread once data is fetched"""
        # smartctl returns 0/1/2/4/8/… via bitmask; >2 usually means real error
        if code == -1 or (not stdout and code > 2):
            self._status_page.set_icon_name("dialog-error-symbolic")
            self._status_page.set_title("Could Not Read SMART Data")
            msg = stderr or "Unknown error"
            if "pkexec" in msg.lower() or "permission" in msg.lower():
                msg = "Root privileges required.\nThe app will prompt for your password via pkexec."
            self._status_page.set_description(msg)
            self._stack.set_visible_child_name("placeholder")
            return

        data = SmartParser.parse(stdout)
        self._render(data, device)

    # rendering

    def _render(self, data: dict, device: str):
        """build the full UI from parsed data"""
        # clear old content
        while True:
            child = self._content_box.get_first_child()
            if child is None:
                break
            self._content_box.remove(child)

        box = self._content_box

        # drive info
        box.append(make_section_label("Drive Information"))
        info_group = Adw.PreferencesGroup()
        di = data["drive_info"]

        info_group.add(make_info_row("Device", device))
        info_group.add(make_info_row("Model",    di.get("model", "Unknown")))
        info_group.add(make_info_row("Serial",   di.get("serial", "Unknown")))
        info_group.add(make_info_row("Firmware", di.get("firmware", "Unknown")))
        info_group.add(make_info_row("Capacity", di.get("capacity", "Unknown")))
        if "rpm" in di:
            info_group.add(make_info_row("Rotation Rate", di["rpm"]))
        if "form" in di:
            info_group.add(make_info_row("Form Factor", di["form"]))
        box.append(info_group)

        # health status
        box.append(make_section_label("Health Status"))
        health_group = Adw.PreferencesGroup()

        health = data["health"]
        overall = health.get("overall", "UNKNOWN")
        passed  = health.get("passed")

        status_row = Adw.ActionRow()
        status_row.set_title("Overall SMART Status")

        if passed is True:
            badge = make_status_badge("PASSED", "success")
        elif passed is False:
            badge = make_status_badge("FAILED", "error")
        else:
            badge = make_status_badge("UNKNOWN", "neutral")

        badge.set_valign(Gtk.Align.CENTER)
        status_row.add_suffix(badge)
        health_group.add(status_row)

        if health.get("failure_predicted"):
            warn_row = Adw.ActionRow()
            warn_row.set_icon_name("dialog-warning-symbolic")
            warn_row.set_title("Drive failure predicted")
            warn_row.set_subtitle("Back up your data immediately! Drive failure expected in less than 24 hours.")
            warn_row.add_css_class("error")
            health_group.add(warn_row)

        box.append(health_group)

        # key metrics (cards)
        box.append(make_section_label("Key Metrics"))
        metrics_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        metrics_box.set_homogeneous(True)

        m = data["metrics"]
        self._add_metric_card(metrics_box, "drive-harddisk-symbolic",
            "Temperature",
            self._fmt_temp(m.get("temperature")),
            self._temp_style(m.get("temperature")))

        self._add_metric_card(metrics_box, "clock-symbolic",
            "Power-On Hours",
            self._fmt_hours(m.get("power_on_hours")),
            "")

        self._add_metric_card(metrics_box, "emblem-important-symbolic",
            "Reallocated Sectors",
            str(m.get("reallocated_sectors", "—")),
            "error" if int(m.get("reallocated_sectors") or 0) > 0 else "success")

        self._add_metric_card(metrics_box, "dialog-warning-symbolic",
            "Pending Sectors",
            str(m.get("pending_sectors", "—")),
            "warning" if int(m.get("pending_sectors") or 0) > 0 else "success")

        box.append(metrics_box)

        # smart attributes table
        if data["attributes"]:
            box.append(make_section_label("SMART Attributes"))
            box.append(self._build_attr_table(data["attributes"]))

        # error log
        box.append(make_section_label("Error Log"))
        err_group = Adw.PreferencesGroup()

        if not data["errors"] or data["health"].get("no_errors"):
            ok_row = Adw.ActionRow()
            ok_row.set_title("No errors logged")
            ok_row.set_icon_name("emblem-ok-symbolic")
            err_group.add(ok_row)
        else:
            for err in data["errors"][:10]:  # limit to 10
                row = Adw.ExpanderRow()
                row.set_title(f"Error #{err['num']}")
                row.set_icon_name("dialog-error-symbolic")
                for detail in err["details"][:6]:
                    sub = Adw.ActionRow()
                    sub.set_title(detail)
                    row.add_row(sub)
                err_group.add(row)

        box.append(err_group)

        # raw output expander
        raw_expander = Adw.ExpanderRow()
        raw_expander.set_title("Raw smartctl Output")
        raw_expander.set_icon_name("text-x-generic-symbolic")

        raw_tv = Gtk.TextView()
        raw_tv.set_editable(False)
        raw_tv.set_monospace(True)
        raw_tv.set_wrap_mode(Gtk.WrapMode.CHAR)
        raw_tv.set_margin_start(8)
        raw_tv.set_margin_end(8)
        raw_tv.set_margin_top(8)
        raw_tv.set_margin_bottom(8)
        raw_tv.get_buffer().set_text(data["raw"])

        raw_scroll = Gtk.ScrolledWindow()
        raw_scroll.set_min_content_height(200)
        raw_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        raw_scroll.set_child(raw_tv)

        raw_row = Adw.ActionRow()
        raw_row.set_activatable(False)
        raw_row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        raw_row_box.append(raw_scroll)
        raw_expander.add_row(raw_row)
        raw_expander.add_row(Adw.ActionRow())  # spacer trick - replace with custom widget
        # actually just add the scroll directly via a workaround
        raw_expander_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        raw_expander_box.append(raw_scroll)

        outer = Adw.PreferencesGroup()
        outer.add(raw_expander)
        # inject the raw scroll as a child row
        raw_child_row = Adw.ActionRow()
        raw_child_row.set_activatable(False)
        raw_child_row.set_child(raw_scroll)  # not standard but works as fallback

        box.append(outer)

        # timestamp
        ts = Gtk.Label(label=f"Last updated: {datetime.now().strftime('%H:%M:%S')}")
        ts.set_halign(Gtk.Align.END)
        ts.set_margin_top(8)
        ts.add_css_class("dim-label")
        box.append(ts)

        self._stack.set_visible_child_name("content")

    def _add_metric_card(self, parent, icon, label, value, style):
        """add a metric card widget to a horizontal box"""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.set_halign(Gtk.Align.FILL)
        card.set_valign(Gtk.Align.CENTER)
        card.add_css_class("card")
        card.add_css_class("metric-card")
        card.set_margin_top(4)
        card.set_margin_bottom(4)

        icon_img = Gtk.Image.new_from_icon_name(icon)
        icon_img.set_icon_size(Gtk.IconSize.NORMAL)
        icon_img.add_css_class("dim-label")
        card.append(icon_img)

        val_lbl = Gtk.Label(label=str(value))
        val_lbl.add_css_class("metric-value")
        val_lbl.add_css_class("title-1")
        if style == "error":
            val_lbl.add_css_class("error")
        elif style == "warning":
            val_lbl.add_css_class("warning")
        elif style == "success":
            val_lbl.add_css_class("success")
        elif style:
            val_lbl.add_css_class(style)
        card.append(val_lbl)

        lbl = Gtk.Label(label=label)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        card.append(lbl)

        parent.append(card)

    def _build_attr_table(self, attributes: list) -> Gtk.Widget:
        """build a scrollable column-view for SMART attributes."""
        # use a ListBox for compatibility, real ColumnView for GTK4
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")

        # header row
        header_row = Gtk.ListBoxRow()
        header_row.set_selectable(False)
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_box.set_margin_top(6)
        header_box.set_margin_bottom(6)
        header_box.set_margin_start(8)
        header_box.set_margin_end(8)

        for col, width in [("ID", 40), ("Attribute", 220), ("Value", 55),
                           ("Worst", 55), ("Thresh", 55), ("Raw", 120)]:
            lbl = Gtk.Label(label=col)
            lbl.set_size_request(width, -1)
            lbl.set_halign(Gtk.Align.START)
            lbl.add_css_class("heading")
            header_box.append(lbl)

        header_row.set_child(header_box)
        list_box.append(header_row)

        for attr in attributes:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)

            if attr["critical"]:
                row.add_css_class("attr-row-critical")
            elif attr["thresh"] > 0 and attr["value"] < attr["thresh"] * 1.2:
                row.add_css_class("attr-row-warn")

            rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            rbox.set_margin_top(4)
            rbox.set_margin_bottom(4)
            rbox.set_margin_start(8)
            rbox.set_margin_end(8)

            def make_col(text, width, bold=False, css=""):
                l = Gtk.Label(label=str(text))
                l.set_size_request(width, -1)
                l.set_halign(Gtk.Align.START)
                l.set_ellipsize(Pango.EllipsizeMode.END)
                if bold:
                    l.add_css_class("bold")
                if css:
                    l.add_css_class(css)
                return l

            rbox.append(make_col(attr["id"],    40))
            rbox.append(make_col(attr["name"],  220))
            rbox.append(make_col(attr["value"], 55))
            rbox.append(make_col(attr["worst"], 55))
            rbox.append(make_col(attr["thresh"],55))

            # raw value: highlight if critical
            raw_lbl = make_col(attr["raw"], 120, bold=attr["critical"],
                               css="error" if attr["critical"] else "")
            rbox.append(raw_lbl)

            row.set_child(rbox)
            list_box.append(row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(300)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(list_box)
        return scroll

    # helpers

    def _fmt_temp(self, temp) -> str:
        if temp is None:
            return "—"
        return f"{temp}°C"

    def _temp_style(self, temp) -> str:
        if temp is None:
            return ""
        if temp >= 55:
            return "temp-hot"
        if temp >= 45:
            return "temp-warm"
        return "temp-ok"

    def _fmt_hours(self, hours) -> str:
        if hours is None:
            return "—"
        h = int(str(hours).replace(",", ""))
        days = h // 24
        return f"{days}d {h % 24}h" if days > 0 else f"{h}h"

    def _show_toast(self, message: str):
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        # find overlay, we use a simple dialog fallback
        overlay = Adw.ToastOverlay()
        # since we don't have a ToastOverlay in the hierarchy, print to console
        print(f"[Toast] {message}")


# entry point

class DriveHealthApp(Adw.Application):

    def __init__(self):
        super().__init__(
            application_id="com.mschiller890.smartreader",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        win = DriveHealthWindow(application=app)
        win.present()


def main():
    app = DriveHealthApp()
    return app.run(None)


if __name__ == "__main__":
    import sys
    sys.exit(main())
