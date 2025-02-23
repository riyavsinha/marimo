import { atom, useSetAtom } from "jotai";

export interface Suggestion {
  id: string;
  title: string;
  description: string;
  type: "prompt_idea" | "prompt_warning";
}

export const suggestionsAtom = atom<Suggestion[]>([]);

export const useSuggestionsActions = () => {
  const setSuggestions = useSetAtom(suggestionsAtom);

  return {
    addSuggestions: (suggestions: Suggestion[]) => {
      setSuggestions((prev) => [...prev, ...suggestions]);
    },
    clearSuggestions: () => {
      setSuggestions([]);
    },
    setSuggestions: (suggestions: Suggestion[]) => {
      setSuggestions(suggestions);
    },
  };
};
