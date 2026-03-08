from __future__ import annotations

from typing import Callable, Protocol


# Options can be (label, value) or (label, value, hint)
type SelectOption = tuple[str, str] | tuple[str, str, str]


class WizardPrompter(Protocol):
    """Decouples wizard logic from terminal I/O for testability."""

    def intro(self, title: str) -> None: ...

    def outro(self, message: str) -> None: ...

    def note(self, message: str, *, title: str = "") -> None: ...

    def section(self, step: int, title: str) -> None: ...

    def text(
        self,
        message: str,
        *,
        default: str = "",
        placeholder: str = "",
        validate: Callable[[str], str | None] | None = None,
    ) -> str: ...

    def password(
        self,
        message: str,
        *,
        validate: Callable[[str], str | None] | None = None,
    ) -> str: ...

    def select(
        self,
        message: str,
        *,
        options: list[SelectOption],
        default: str = "",
    ) -> str: ...

    def multi_select(
        self,
        message: str,
        *,
        options: list[SelectOption],
        defaults: list[str] | None = None,
    ) -> list[str]: ...

    def confirm(self, message: str, *, default: bool = False) -> bool: ...


def _parse_option(opt: SelectOption) -> tuple[str, str, str]:
    """Extract (label, value, hint) from a 2-tuple or 3-tuple."""
    if len(opt) == 3:
        return opt[0], opt[1], opt[2]
    return opt[0], opt[1], ""


class TerminalPrompter:
    """Interactive prompter backed by questionary."""

    def intro(self, title: str) -> None:
        import questionary

        questionary.print(f"\n{'=' * 50}", style="bold")
        questionary.print(f"  {title}", style="bold")
        questionary.print(f"{'=' * 50}\n", style="bold")

    def outro(self, message: str) -> None:
        import questionary

        questionary.print(f"\n{message}\n", style="bold fg:green")

    def note(self, message: str, *, title: str = "") -> None:
        import questionary

        if title:
            questionary.print(f"\n  [{title}] {message}", style="fg:cyan")
        else:
            questionary.print(f"\n  {message}", style="fg:cyan")

    def section(self, step: int, title: str) -> None:
        import questionary

        label = f" Step {step}: {title} "
        line = "─" * max(0, 50 - len(label))
        questionary.print(f"\n──{label}{line}\n", style="bold fg:ansiyellow")

    def text(
        self,
        message: str,
        *,
        default: str = "",
        placeholder: str = "",
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        import questionary

        def _validate(val: str) -> bool | str:
            if validate is None:
                return True
            err = validate(val)
            return err if err is not None else True

        result = questionary.text(
            message,
            default=default,
            validate=_validate,
        ).ask()
        if result is None:
            raise KeyboardInterrupt
        return result

    def password(
        self,
        message: str,
        *,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        import questionary

        def _validate(val: str) -> bool | str:
            if validate is None:
                return True
            err = validate(val)
            return err if err is not None else True

        result = questionary.password(message, validate=_validate).ask()
        if result is None:
            raise KeyboardInterrupt
        return result

    def select(
        self,
        message: str,
        *,
        options: list[SelectOption],
        default: str = "",
    ) -> str:
        import questionary

        choices = []
        for opt in options:
            label, value, hint = _parse_option(opt)
            if hint:
                choices.append(questionary.Choice(title=label, value=value, description=hint))
            else:
                choices.append(questionary.Choice(title=label, value=value))
        result = questionary.select(message, choices=choices, default=default or None).ask()
        if result is None:
            raise KeyboardInterrupt
        return result

    def multi_select(
        self,
        message: str,
        *,
        options: list[SelectOption],
        defaults: list[str] | None = None,
    ) -> list[str]:
        import questionary

        default_set = set(defaults or [])
        choices = []
        for opt in options:
            label, value, hint = _parse_option(opt)
            kwargs: dict = {"title": label, "value": value, "checked": value in default_set}
            if hint:
                kwargs["description"] = hint
            choices.append(questionary.Choice(**kwargs))
        result = questionary.checkbox(message, choices=choices).ask()
        if result is None:
            raise KeyboardInterrupt
        return result

    def confirm(self, message: str, *, default: bool = False) -> bool:
        import questionary

        result = questionary.confirm(message, default=default).ask()
        if result is None:
            raise KeyboardInterrupt
        return result


class MockPrompter:
    """Test double: pops pre-programmed answers in order."""

    def __init__(self, answers: list) -> None:
        self._answers = list(answers)
        self._pos = 0

    def _pop(self):
        if self._pos >= len(self._answers):
            raise AssertionError(
                f"MockPrompter exhausted after {self._pos} answers"
            )
        val = self._answers[self._pos]
        self._pos += 1
        return val

    @property
    def remaining(self) -> int:
        return len(self._answers) - self._pos

    def intro(self, title: str) -> None:
        pass

    def outro(self, message: str) -> None:
        pass

    def note(self, message: str, *, title: str = "") -> None:
        pass

    def section(self, step: int, title: str) -> None:
        pass

    def text(self, message: str, **kwargs) -> str:
        return str(self._pop())

    def password(self, message: str, **kwargs) -> str:
        return str(self._pop())

    def select(self, message: str, **kwargs) -> str:
        return str(self._pop())

    def multi_select(self, message: str, **kwargs) -> list[str]:
        val = self._pop()
        return val if isinstance(val, list) else [val]

    def confirm(self, message: str, **kwargs) -> bool:
        return bool(self._pop())
