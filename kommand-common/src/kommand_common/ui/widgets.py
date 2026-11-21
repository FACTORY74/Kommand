"""Reusable Kommand desktop controls."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


def clamp_number(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


class NumberStepper(ttk.Frame):
    """Horizontal minus/value/plus integer input with large targets."""

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        *,
        minimum: int = 0,
        maximum: int = 100,
        step: int = 1,
        width: int = 4,
        zero_pad: int = 0,
        command: Callable[[int], None] | None = None,
        button_size: int = 38,
        allow_invalid: bool = False,
    ) -> None:
        super().__init__(parent, style="Stepper.TFrame")
        if minimum > maximum or step <= 0:
            raise ValueError("Invalid NumberStepper bounds or step.")
        self.variable = variable
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.zero_pad = zero_pad
        self.command = command
        self.allow_invalid = allow_invalid

        minus_segment = ttk.Frame(
            self, width=button_size, height=button_size, style="StepperSegment.TFrame"
        )
        minus_segment.grid(row=0, column=0)
        minus_segment.grid_propagate(False)
        self.minus = ttk.Button(
            minus_segment,
            text="−",
            command=lambda: self.adjust(-self.step),
            style="Stepper.TButton",
            takefocus=True,
        )
        self.minus.grid(row=0, column=0, sticky="nsew")
        minus_segment.rowconfigure(0, weight=1)
        minus_segment.columnconfigure(0, weight=1)

        value_width = max(44, width * 9 + 18)
        value_segment = ttk.Frame(
            self, width=value_width, height=button_size, style="StepperSegment.TFrame"
        )
        value_segment.grid(row=0, column=1)
        value_segment.grid_propagate(False)
        self.entry = ttk.Entry(
            value_segment,
            textvariable=variable,
            justify="center",
            style="Stepper.TEntry",
        )
        self.entry.grid(row=0, column=0, sticky="nsew")
        value_segment.rowconfigure(0, weight=1)
        value_segment.columnconfigure(0, weight=1)

        plus_segment = ttk.Frame(
            self, width=button_size, height=button_size, style="StepperSegment.TFrame"
        )
        plus_segment.grid(row=0, column=2)
        plus_segment.grid_propagate(False)
        self.plus = ttk.Button(
            plus_segment,
            text="+",
            command=lambda: self.adjust(self.step),
            style="Stepper.TButton",
            takefocus=True,
        )
        self.plus.grid(row=0, column=0, sticky="nsew")
        plus_segment.rowconfigure(0, weight=1)
        plus_segment.columnconfigure(0, weight=1)

        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<FocusOut>", self._commit)
        self.entry.bind("<Up>", lambda event: self._key_adjust(event, self.step))
        self.entry.bind("<Down>", lambda event: self._key_adjust(event, -self.step))
        self.entry.bind("<MouseWheel>", self._wheel)
        self._commit()

    def get(self) -> int:
        try:
            return clamp_number(int(self.variable.get()), self.minimum, self.maximum)
        except ValueError:
            return self.minimum

    def set(self, value: int, notify: bool = False) -> None:
        value = clamp_number(value, self.minimum, self.maximum)
        self.variable.set(f"{value:0{self.zero_pad}d}" if self.zero_pad else str(value))
        self.minus.configure(state="disabled" if value <= self.minimum else "normal")
        self.plus.configure(state="disabled" if value >= self.maximum else "normal")
        if notify and self.command:
            self.command(value)

    def adjust(self, delta: int) -> None:
        self.set(self.get() + delta, notify=True)

    def _commit(self, _event: object = None) -> str | None:
        if self.allow_invalid:
            try:
                value = int(self.variable.get())
            except ValueError:
                return "break" if _event is not None else None
            if not self.minimum <= value <= self.maximum:
                return "break" if _event is not None else None
        self.set(self.get(), notify=True)
        return "break" if _event is not None else None

    def _key_adjust(self, _event: object, delta: int) -> str:
        self.adjust(delta)
        return "break"

    def _wheel(self, event: tk.Event) -> str:
        self.adjust(self.step if event.delta > 0 else -self.step)
        return "break"
