"""Material-inspired controls shared by Kommand desktop applications."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


COLORS = {
    "background": "#101318",
    "surface": "#191D24",
    "surface_high": "#222832",
    "border": "#303844",
    "text": "#F2F5F8",
    "muted": "#9AA6B2",
    "primary": "#5AA7FF",
    "primary_hover": "#80BAFF",
    "primary_pressed": "#3689E6",
    "danger": "#FF6B7A",
    "selection": "#244B73",
}


def apply_theme(master: tk.Tk) -> None:
    colors = COLORS
    master.configure(background=colors["background"])
    style = ttk.Style(master)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), foreground=colors["text"])
    style.configure("TFrame", background=colors["surface"])
    style.configure("App.TFrame", background=colors["background"])
    style.configure("Card.TFrame", background=colors["surface"])
    style.configure("TLabel", background=colors["surface"], foreground=colors["muted"])
    style.configure(
        "PageTitle.TLabel",
        background=colors["background"],
        foreground=colors["text"],
        font=("Segoe UI Semibold", 18),
    )
    style.configure("Status.TLabel", background=colors["surface_high"], foreground=colors["text"], padding=(12, 7), font=("Segoe UI Semibold", 9))
    style.configure("Card.TLabelframe", background=colors["surface"], bordercolor=colors["border"], relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=colors["surface"], foreground=colors["primary"], font=("Segoe UI Semibold", 9))
    style.configure("TEntry", fieldbackground=colors["surface_high"], foreground=colors["text"], insertcolor=colors["text"], bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"], padding=(9, 7))
    style.map("TEntry", bordercolor=[("focus", colors["primary"])], lightcolor=[("focus", colors["primary"])], darkcolor=[("focus", colors["primary"])])
    style.configure(
        "TCombobox",
        fieldbackground=colors["surface_high"],
        background=colors["surface_high"],
        foreground=colors["text"],
        arrowcolor=colors["primary"],
        bordercolor=colors["border"],
        padding=(7, 7),
    )
    _install_combobox_arrow(master, style)
    style.map("TCombobox", fieldbackground=[("readonly", colors["surface_high"])], foreground=[("readonly", colors["text"])], bordercolor=[("focus", colors["primary"])])
    master.option_add("*TCombobox*Listbox.background", colors["surface_high"])
    master.option_add("*TCombobox*Listbox.foreground", colors["text"])
    master.option_add("*TCombobox*Listbox.selectBackground", colors["selection"])
    style.configure("TButton", background=colors["surface_high"], foreground=colors["text"], borderwidth=0, padding=(14, 9), font=("Segoe UI Semibold", 9))
    style.map("TButton", background=[("active", colors["border"]), ("pressed", colors["selection"])], foreground=[("disabled", colors["muted"])])
    style.configure("Accent.TButton", background=colors["primary"], foreground="#07111E")
    style.map("Accent.TButton", background=[("active", colors["primary_hover"]), ("pressed", colors["primary_pressed"]), ("disabled", colors["border"])], foreground=[("disabled", colors["muted"])])
    style.configure("Danger.TButton", background=colors["surface_high"], foreground=colors["danger"])
    style.map("Danger.TButton", background=[("active", "#3A2830")])
    style.configure("TCheckbutton", background=colors["surface"], foreground=colors["text"], indicatorcolor=colors["surface_high"], padding=(4, 6))
    style.map("TCheckbutton", indicatorcolor=[("selected", colors["primary"])], background=[("active", colors["surface"])])
    style.configure("Treeview", background=colors["surface_high"], fieldbackground=colors["surface_high"], foreground=colors["text"], bordercolor=colors["border"], rowheight=32, font=("Segoe UI", 9))
    style.map("Treeview", background=[("selected", colors["selection"])], foreground=[("selected", colors["text"])])
    style.configure("Treeview.Heading", background=colors["surface"], foreground=colors["muted"], relief="flat", padding=(8, 9), font=("Segoe UI Semibold", 9))
    style.map("Treeview.Heading", background=[("active", colors["surface_high"])])
    style.configure("Stepper.TFrame", background=colors["border"], borderwidth=1, relief="solid")
    style.configure("StepperSegment.TFrame", background=colors["border"], borderwidth=0)
    style.configure("Stepper.TButton", background=colors["surface_high"], foreground=colors["primary"], borderwidth=0, padding=0, anchor="center", font=("Segoe UI Semibold", 12))
    style.map("Stepper.TButton", background=[("active", colors["selection"]), ("pressed", colors["primary_pressed"]), ("disabled", colors["surface"])], foreground=[("disabled", colors["muted"])])
    style.configure("Stepper.TEntry", fieldbackground=colors["surface"], foreground=colors["text"], borderwidth=0, padding=(8, 9), font=("Segoe UI Semibold", 10))
    style.map("Stepper.TEntry", fieldbackground=[("focus", colors["surface_high"])])


def _install_combobox_arrow(master: tk.Tk, style: ttk.Style) -> None:
    """Install a square dropdown segment with a small, controlled chevron."""
    normal = _dropdown_image(master, COLORS["surface_high"])
    hover = _dropdown_image(master, COLORS["selection"])
    pressed = _dropdown_image(master, COLORS["primary_pressed"], arrow="#07111E")
    # Tk images must remain referenced for the lifetime of the root window.
    master._kommand_combobox_images = (normal, hover, pressed)  # type: ignore[attr-defined]
    element = "Kommand.Combobox.downarrow"
    try:
        style.element_create(
            element,
            "image",
            normal,
            ("pressed", pressed),
            ("active", hover),
            border=0,
            sticky="nsew",
        )
    except tk.TclError:
        # The ttk element namespace is process-wide and may already exist in tests.
        pass
    style.layout(
        "TCombobox",
        [
            (
                "Combobox.field",
                {
                    "sticky": "nswe",
                    "children": [
                        (element, {"side": "right", "sticky": "ns"}),
                        (
                            "Combobox.padding",
                            {
                                "sticky": "nswe",
                                "children": [
                                    ("Combobox.textarea", {"sticky": "nswe"}),
                                ],
                            },
                        ),
                    ],
                },
            )
        ],
    )


def _dropdown_image(master: tk.Misc, background: str, arrow: str | None = None) -> tk.PhotoImage:
    size = 30
    image = tk.PhotoImage(master=master, width=size, height=size)
    image.put(background, to=(0, 0, size, size))
    image.put(COLORS["border"], to=(0, 0, 1, size))
    colour = arrow or COLORS["primary"]
    # Nine pixels wide and five high: deliberately much smaller than its hit target.
    for row, width in enumerate((9, 7, 5, 3, 1)):
        left = (size - width) // 2
        image.put(colour, to=(left, 12 + row, left + width, 13 + row))
    return image


def app_header(parent: tk.Misc, title: str) -> ttk.Frame:
    frame = ttk.Frame(parent, style="App.TFrame")
    ttk.Label(frame, text=title, style="PageTitle.TLabel").pack(anchor="w")
    return frame


def card(parent: tk.Misc, title: str, padding: tuple[int, int] = (18, 14)) -> ttk.LabelFrame:
    return ttk.LabelFrame(parent, text=f"  {title.upper()}  ", padding=padding, style="Card.TLabelframe")


def action_button(parent: tk.Misc, text: str, command, kind: str = "normal") -> ttk.Button:
    style = {"primary": "Accent.TButton", "danger": "Danger.TButton"}.get(kind, "TButton")
    return ttk.Button(parent, text=text.upper(), command=command, style=style)
