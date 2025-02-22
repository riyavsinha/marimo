from typing import TYPE_CHECKING, List

from marimo._messaging.ops import Suggestions

if TYPE_CHECKING:
    from marimo._ai.suggestions import Suggestion


class SuggestionsManager:
    """Manages and broadcasts suggestions to the frontend."""

    def __init__(self) -> None:
        pass

    def add_suggestions(self, suggestions: List[Suggestion]) -> None:
        """Add suggestions to the manager.

        Args:
            suggestions: List of suggestions to send to the frontend
        """
        Suggestions(suggestions=suggestions).broadcast()
