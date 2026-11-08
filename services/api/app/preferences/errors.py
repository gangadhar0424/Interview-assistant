"""Preference validation errors. They name the field only, never echo the submitted value."""


class InvalidPreference(ValueError):
    def __init__(self, field: str) -> None:
        super().__init__(f"invalid value for {field}")
        self.field = field
