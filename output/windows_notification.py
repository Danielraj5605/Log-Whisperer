"""Windows toast notifications using the Windows Runtime."""

from __future__ import annotations

import sys
from typing import Any
from xml.etree import ElementTree as ET

from output import _fmt

class WindowsNotifier:
    """Build a toast using XML text nodes and the native Windows Runtime."""

    def notify(self, finding: dict[str, Any]) -> None:
        if sys.platform != "win32":
            return
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager

        document = XmlDocument()
        document.load_xml(self._xml(finding))
        ToastNotificationManager.create_toast_notifier("Log Whisperer").show(ToastNotification(document))

    def _xml(self, finding: dict[str, Any]) -> str:
        root = ET.Element("toast")
        binding = ET.SubElement(ET.SubElement(root, "visual"), "binding", template="ToastGeneric")
        for value in (self._title(finding), self._body(finding)):
            ET.SubElement(binding, "text").text = value
        return ET.tostring(root, encoding="unicode")

    def _title(self, finding: dict[str, Any]) -> str:
        confidence = _fmt.confidence(finding)
        source = _fmt.text(finding, "source", "unknown")
        trigger = _fmt.text(finding, "trigger", "alert")
        emoji = "🔴" if confidence == "high" else "🟡" if confidence == "medium" else "🔵"
        return f"{emoji} Log Whisperer — {trigger} ({source})"

    def _body(self, finding: dict[str, Any]) -> str:
        lines = [_fmt.text(finding, "root_signal", "unknown", 100)]
        caused = _fmt.text(finding, "caused_by", "", 100)
        action = _fmt.text(finding, "action", "", 100)
        if caused:
            lines.append(caused)
        if action:
            lines.append(f"→ {action}")
        return "\n".join(lines)
